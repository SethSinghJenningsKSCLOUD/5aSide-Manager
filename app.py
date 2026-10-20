import os
from datetime import date, datetime, time, timedelta
from functools import wraps  # <--- ADD THIS LINE
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
from supabase import Client, create_client
from team_balancer import generate_balanced_teams, update_elo_ratings

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key-change-me")
app.permanent_session_lifetime = timedelta(days=90)

ADMIN_PIN = os.getenv("ADMIN_PIN", "0512")


url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)

ELO_MAP = {"1": 700, "2": 850, "3": 1000, "4": 1150, "5": 1300}

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    next_url = request.args.get("next") or url_for("index")
    error = None

    if request.method == "POST":
        entered_pin = request.form.get("pin", "").strip()
        if entered_pin == ADMIN_PIN:
            session.permanent = True
            session["is_admin"] = True
            return redirect(request.form.get("next") or url_for("index"))
        else:
            error = "Incorrect PIN. Try again."

    return render_template("login.html", next_url=next_url, error=error)


@app.route("/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("rsvp_page"))

def get_upcoming_monday():
    today = date.today()
    days_until_monday = (0 - today.weekday()) % 7
    return today + timedelta(days=days_until_monday)


def sync_admin_attendance(selected_ids: list, upcoming_date):
    """Synchronizes admin selection with the availability table and logs late

    dropouts.
    """
    avail_resp = (
        supabase.table("availability")
        .select("*")
        .eq("match_date", str(upcoming_date))
        .execute()
    )
    current_in_ids = [
        row["player_id"]
        for row in (avail_resp.data or [])
        if row["status"] == "IN"
    ]

    # 1. Identify players who were IN but got unchecked by admin (Dropouts)
    dropped_ids = [pid for pid in current_in_ids if pid not in selected_ids]
    if dropped_ids:
        match_datetime = datetime.combine(upcoming_date, time(19, 0))
        hours_until = (match_datetime - datetime.now()).total_seconds() / 3600
        is_late = hours_until <= 24

        for pid in dropped_ids:
            supabase.table("availability").upsert(
                {
                    "player_id": str(pid),
                    "match_date": str(upcoming_date),
                    "status": "OUT",
                    "updated_at": datetime.now().isoformat(),
                },
                on_conflict="player_id, match_date",
            ).execute()

            supabase.table("flake_logs").insert(
                {
                    "player_id": str(pid),
                    "match_date": str(upcoming_date),
                    "hours_before_match": round(hours_until, 1),
                    "is_late": is_late,
                }
            ).execute()

    # 2. Identify newly checked players and set them to IN
    for pid in selected_ids:
        if pid not in current_in_ids:
            supabase.table("availability").upsert(
                {
                    "player_id": str(pid),
                    "match_date": str(upcoming_date),
                    "status": "IN",
                    "updated_at": datetime.now().isoformat(),
                },
                on_conflict="player_id, match_date",
            ).execute()


def get_player_stats():
    matches_res = (
        supabase.table("matches")
        .select("*")
        .eq("is_completed", True)
        .order("match_date", desc=True)
        .execute()
    )
    matches = matches_res.data or []
    total_matches = len(matches)

    attendance_res = supabase.table("match_attendance").select("*").execute()
    attendance = attendance_res.data or []

    match_att_map = {}
    for att in attendance:
        m_id = att["match_id"]
        if m_id not in match_att_map:
            match_att_map[m_id] = []
        match_att_map[m_id].append(att)

    players_res = supabase.table("players").select("id").execute()
    all_p_ids = [p["id"] for p in (players_res.data or [])]

    # Tally MVP awards
    mvp_counts = {}
    recent_mvp_id = matches[0].get("mvp_player_id") if matches else None
    for m in matches:
        mvp_id = m.get("mvp_player_id")
        if mvp_id:
            mvp_counts[mvp_id] = mvp_counts.get(mvp_id, 0) + 1

    player_stats = {}

    for p_id in all_p_ids:
        attn_streak = 0
        win_streak = 0
        appearances = 0
        wins = 0

        win_streak_active = True
        attn_streak_active = True

        for m in matches:
            m_id = m["id"]
            m_atts = match_att_map.get(m_id, [])
            p_att = next((a for a in m_atts if a["player_id"] == p_id), None)

            if p_att:
                appearances += 1
                team = p_att["team_assigned"]
                score_a = m.get("team_a_score", 0)
                score_b = m.get("team_b_score", 0)
                won = (team == "A" and score_a > score_b) or (
                    team == "B" and score_b > score_a
                )

                if won:
                    wins += 1

                if attn_streak_active:
                    attn_streak += 1

                if win_streak_active:
                    if won:
                        win_streak += 1
                    else:
                        win_streak_active = False
            else:
                attn_streak_active = False
                win_streak_active = False

        win_pct = (
            round((wins / appearances) * 100, 1) if appearances >= 10 else None
        )
        attn_pct = (
            round((appearances / total_matches) * 100, 1)
            if (total_matches >= 10 and appearances >= 10)
            else None
        )

        player_stats[p_id] = {
            "attn_streak": attn_streak,
            "win_streak": win_streak,
            "appearances": appearances,
            "win_pct": win_pct,
            "attn_pct": attn_pct,
            "mvp_count": mvp_counts.get(p_id, 0),
            "is_recent_mvp": (p_id == recent_mvp_id),
        }

    return player_stats

@app.route("/")
@admin_required
def index():
    upcoming_date = get_upcoming_monday()
    response = supabase.table("players").select("*").order("name").execute()
    players = response.data or []
    player_map = {p["id"]: p["name"] for p in players}

    avail_resp = (
        supabase.table("availability")
        .select("*")
        .eq("match_date", str(upcoming_date))
        .execute()
    )
    in_player_ids = [
        row["player_id"]
        for row in (avail_resp.data or [])
        if row["status"] == "IN"
    ]

    for p in players:
        p["auto_select"] = p["id"] in in_player_ids

    late_flakes_resp = (
        supabase.table("flake_logs")
        .select("*, players(name)")
        .eq("match_date", str(upcoming_date))
        .eq("is_late", True)
        .order("dropped_at", desc=True)
        .execute()
    )
    late_alerts = late_flakes_resp.data or []

    # 1. Check for an active, uncompleted match draft (persisted teams)
    draft_res = (
        supabase.table("matches")
        .select("*")
        .eq("match_date", str(upcoming_date))
        .eq("is_completed", False)
        .execute()
    )
    draft_match = draft_res.data[0] if draft_res.data else None
    match_result = None

    if draft_match:
        att_res = (
            supabase.table("match_attendance")
            .select("player_id, team_assigned, players(id, name, elo_rating)")
            .eq("match_id", draft_match["id"])
            .execute()
        )
        team_a_players = []
        team_b_players = []
        for row in (att_res.data or []):
            p = row.get("players")
            if p:
                if row["team_assigned"] == "A":
                    team_a_players.append(p)
                else:
                    team_b_players.append(p)

        if team_a_players and team_b_players:
            rating_a = sum(p["elo_rating"] for p in team_a_players)
            rating_b = sum(p["elo_rating"] for p in team_b_players)
            match_result = {
                "match_id": draft_match["id"],
                "match_date": str(upcoming_date),
                "team_a": [p["name"] for p in team_a_players],
                "team_b": [p["name"] for p in team_b_players],
                "team_a_ids": [p["id"] for p in team_a_players],
                "team_b_ids": [p["id"] for p in team_b_players],
                "team_a_rating": round(rating_a / len(team_a_players)),
                "team_b_rating": round(rating_b / len(team_b_players)),
                "rating_diff": round(abs(rating_a - rating_b)),
                "selected_players": team_a_players + team_b_players,
            }

    # 2. Check for most recent completed match needing Star Player award
    recent_match_res = (
        supabase.table("matches")
        .select("*")
        .eq("is_completed", True)
        .order("match_date", desc=True)
        .limit(1)
        .execute()
    )
    last_completed = recent_match_res.data[0] if recent_match_res.data else None
    pending_mvp_poll = None

    if last_completed and not last_completed.get("mvp_player_id"):
        # Fetch attendees of the match
        att_res = (
            supabase.table("match_attendance")
            .select("player_id, players(id, name)")
            .eq("match_id", last_completed["id"])
            .execute()
        )
        attendees = [
            {"id": row["players"]["id"], "name": row["players"]["name"]}
            for row in (att_res.data or [])
            if row.get("players")
        ]

        # Fetch raw votes (clean, flat select without joins)
        votes_res = (
            supabase.table("mvp_votes")
            .select("voter_id, voted_for_id")
            .eq("match_id", last_completed["id"])
            .execute()
        )
        votes_list = votes_res.data or []

        # Tally votes for every attendee
        tally = {p["id"]: {"id": p["id"], "name": p["name"], "votes": 0} for p in attendees}
        for v in votes_list:
            tgt = v.get("voted_for_id")
            if tgt in tally:
                tally[tgt]["votes"] += 1
            elif tgt in player_map:
                tally[tgt] = {"id": tgt, "name": player_map[tgt], "votes": 1}

        ranked_candidates = sorted(tally.values(), key=lambda x: x["votes"], reverse=True)
        pending_mvp_poll = {
            "match": last_completed,
            "results": ranked_candidates,
            "total_votes": len(votes_list),
        }

    return render_template(
        "index.html",
        players=players,
        match_result=match_result,
        late_alerts=late_alerts,
        upcoming_date=upcoming_date,
        pending_mvp_poll=pending_mvp_poll,
    )

@app.route("/sync-attendance", methods=["POST"])
@admin_required
def sync_attendance():
    selected_ids = request.form.getlist("available_players")
    upcoming_date = get_upcoming_monday()
    sync_admin_attendance(selected_ids, upcoming_date)
    return redirect(url_for("index"))


@app.route("/generate", methods=["POST"])
@admin_required
def generate():
    selected_ids = request.form.getlist("available_players")
    upcoming_date = get_upcoming_monday()

    if not selected_ids:
        return redirect(url_for("index"))

    sync_admin_attendance(selected_ids, upcoming_date)

    response = (
        supabase.table("players").select("*").in_("id", selected_ids).execute()
    )
    selected_players = response.data

    player_pool = {p["name"]: p["elo_rating"] for p in selected_players}
    matchup = generate_balanced_teams(player_pool)

    name_to_id = {p["name"]: p["id"] for p in selected_players}
    team_a_ids = [name_to_id[name] for name in matchup["team_a"]]
    team_b_ids = [name_to_id[name] for name in matchup["team_b"]]

    # Save match draft into Supabase (persists across reloads)
    draft_data = {
        "match_date": str(upcoming_date),
        "team_a_score": 0,
        "team_b_score": 0,
        "is_completed": False,
    }
    match_res = supabase.table("matches").upsert(draft_data, on_conflict="match_date").execute()
    match_id = match_res.data[0]["id"]

    supabase.table("match_attendance").delete().eq("match_id", match_id).execute()
    attendance_records = [
        {"match_id": match_id, "player_id": pid, "team_assigned": "A"}
        for pid in team_a_ids
    ] + [
        {"match_id": match_id, "player_id": pid, "team_assigned": "B"}
        for pid in team_b_ids
    ]
    supabase.table("match_attendance").insert(attendance_records).execute()

    return redirect(url_for("index"))
    selected_ids = request.form.getlist("available_players")
    upcoming_date = get_upcoming_monday()

    if not selected_ids:
        return redirect(url_for("index"))

    # Sync manual admin selections to DB and log flakers
    sync_admin_attendance(selected_ids, upcoming_date)

    response = (
        supabase.table("players").select("*").in_("id", selected_ids).execute()
    )
    selected_players = response.data

    player_pool = {p["name"]: p["elo_rating"] for p in selected_players}
    matchup = generate_balanced_teams(player_pool)

    # Attach IDs for score logging
    name_to_id = {p["name"]: p["id"] for p in selected_players}
    matchup["team_a_ids"] = [name_to_id[name] for name in matchup["team_a"]]
    matchup["team_b_ids"] = [name_to_id[name] for name in matchup["team_b"]]
    matchup["selected_players"] = selected_players

    all_players = (
        supabase.table("players").select("*").order("name").execute().data
    )

    return render_template(
        "index.html",
        players=all_players,
        match_result=matchup,
        late_alerts=[],
        upcoming_date=upcoming_date,
    )

@app.route("/reset-draft", methods=["POST"])
@admin_required
def reset_draft():
    upcoming_date = get_upcoming_monday()
    draft_res = (
        supabase.table("matches")
        .select("id")
        .eq("match_date", str(upcoming_date))
        .eq("is_completed", False)
        .execute()
    )
    if draft_res.data:
        mid = draft_res.data[0]["id"]
        supabase.table("match_attendance").delete().eq("match_id", mid).execute()
        supabase.table("matches").delete().eq("id", mid).execute()
    flash("Teams reset. You can adjust selections and rebalance.")
    return redirect(url_for("index"))

@app.route("/record-score", methods=["POST"])
@admin_required
def record_score():
    team_a_ids = request.form.getlist("team_a_ids")
    team_b_ids = request.form.getlist("team_b_ids")
    score_a = int(request.form.get("score_a", 0))
    score_b = int(request.form.get("score_b", 0))
    short_team = request.form.get("short_team", "none")
    minutes_short = int(request.form.get("minutes_short", 0))

    mvp_id = request.form.get("mvp_id") or None
    top_scorer_ids = request.form.getlist("top_scorer_ids")

    if not team_a_ids or not team_b_ids:
        return redirect(url_for("index"))

    res_a = (
        supabase.table("players").select("*").in_("id", team_a_ids).execute()
    )
    res_b = (
        supabase.table("players").select("*").in_("id", team_b_ids).execute()
    )

    rating_updates = update_elo_ratings(
        res_a.data,
        res_b.data,
        score_a,
        score_b,
        short_team=short_team,
        minutes_short=minutes_short,
        mvp_id=mvp_id,
        top_scorer_ids=top_scorer_ids,
    )

    for player_id, new_elo in rating_updates.items():
        supabase.table("players").update({"elo_rating": new_elo}).eq(
            "id", player_id
        ).execute()

    upcoming_date = get_upcoming_monday()
    match_date = request.form.get("match_date") or str(upcoming_date)

    match_data = {
        "match_date": match_date,
        "team_a_score": score_a,
        "team_b_score": score_b,
        "is_completed": True,
        "short_team": short_team,
        "minutes_short": minutes_short,
        "mvp_player_id": mvp_id,
        "top_scorer_id": top_scorer_ids[0] if top_scorer_ids else None,
    }

    match_res = (
        supabase.table("matches")
        .upsert(match_data, on_conflict="match_date")
        .execute()
    )
    match_id = match_res.data[0]["id"]

    supabase.table("match_attendance").delete().eq("match_id", match_id).execute()
    attendance_records = [
        {"match_id": match_id, "player_id": p_id, "team_assigned": "A"}
        for p_id in team_a_ids
    ] + [
        {"match_id": match_id, "player_id": p_id, "team_assigned": "B"}
        for p_id in team_b_ids
    ]
    supabase.table("match_attendance").insert(attendance_records).execute()
    flash("Match saved & Elo ratings updated! ⚽")
    return redirect(url_for("index"))

@app.route("/finalize-mvp", methods=["POST"])
@admin_required
def finalize_mvp():
    match_id = request.form.get("match_id")
    winner_id = request.form.get("winner_id")

    if not match_id or not winner_id:
        return redirect(url_for("index"))

    match_res = supabase.table("matches").select("mvp_player_id").eq("id", match_id).execute()
    if match_res.data and match_res.data[0].get("mvp_player_id"):
        flash("Star Player already awarded for this match!")
        return redirect(url_for("index"))

    supabase.table("matches").update({"mvp_player_id": winner_id}).eq(
        "id", match_id
    ).execute()

    player_res = (
        supabase.table("players")
        .select("name, elo_rating")
        .eq("id", winner_id)
        .execute()
    )
    if player_res.data:
        winner = player_res.data[0]
        supabase.table("players").update({"elo_rating": winner["elo_rating"] + 20}).eq(
            "id", winner_id
        ).execute()
        flash(f"⭐ {winner['name']} confirmed as Star Player! +20 Elo awarded 🏆")

    return redirect(url_for("index"))

@app.route("/rsvp", methods=["GET"])
def rsvp_page():
    upcoming_date = get_upcoming_monday()
    players = (
        supabase.table("players").select("*").order("name").execute().data or []
    )
    avail_resp = (
        supabase.table("availability")
        .select("*")
        .eq("match_date", str(upcoming_date))
        .execute()
    )
    rsvps = {row["player_id"]: row["status"] for row in (avail_resp.data or [])}

    stats = get_player_stats()
    for p in players:
        p_stat = stats.get(p["id"], {})
        p["attn_streak"] = p_stat.get("attn_streak", 0)
        p["win_streak"] = p_stat.get("win_streak", 0)
        p["appearances"] = p_stat.get("appearances", 0)
        p["win_pct"] = p_stat.get("win_pct")
        p["attn_pct"] = p_stat.get("attn_pct")
        p["mvp_count"] = p_stat.get("mvp_count", 0)
        p["is_recent_mvp"] = p_stat.get("is_recent_mvp", False)

    # 48-Hour Star Player Poll Window
    recent_match_res = (
        supabase.table("matches")
        .select("*")
        .eq("is_completed", True)
        .order("match_date", desc=True)
        .limit(1)
        .execute()
    )
    last_match = recent_match_res.data[0] if recent_match_res.data else None
    active_poll_match = None
    poll_hours_left = None
    match_attendees = []

   # 48-Hour Star Player Poll Window
    recent_match_res = (
        supabase.table("matches")
        .select("*")
        .eq("is_completed", True)
        .order("match_date", desc=True)
        .limit(1)
        .execute()
    )
    last_match = recent_match_res.data[0] if recent_match_res.data else None
    active_poll_match = None
    poll_hours_left = None
    match_attendees = []

    if last_match:
        # Match kickoff assumed at 19:00 on match date
        match_dt = datetime.combine(
            date.fromisoformat(last_match["match_date"]), time(19, 0)
        )
        poll_deadline = match_dt + timedelta(hours=48)
        now = datetime.now()

        # Only activate the poll within the genuine 48-hour window
        if now < poll_deadline:
            active_poll_match = last_match
            poll_hours_left = max(
                1, round((poll_deadline - now).total_seconds() / 3600)
            )

            att_res = (
                supabase.table("match_attendance")
                .select("player_id, players(id, name)")
                .eq("match_id", active_poll_match["id"])
                .execute()
            )
            match_attendees = [
                {"id": row["players"]["id"], "name": row["players"]["name"]}
                for row in (att_res.data or [])
                if row.get("players")
            ]
            match_attendees.sort(key=lambda x: x["name"])

    return render_template(
        "rsvp.html",
        players=players,
        rsvps=rsvps,
        match_date=upcoming_date,
        active_poll_match=active_poll_match,
        poll_hours_left=poll_hours_left,
        match_attendees=match_attendees,
    )
    return render_template(
        "rsvp.html",
        players=players,
        rsvps=rsvps,
        match_date=upcoming_date,
        active_poll_match=active_poll_match,
        poll_hours_left=poll_hours_left,
        match_attendees=match_attendees,
    )

@app.route("/rsvp", methods=["POST"])
def submit_rsvp():
    player_id = request.form.get("player_id")
    status = request.form.get("status")
    upcoming_date = get_upcoming_monday()

    if player_id and status in ["IN", "OUT"]:
        # 1. Fetch previous status and timestamp
        prev_resp = (
            supabase.table("availability")
            .select("status, updated_at")
            .eq("player_id", str(player_id))
            .eq("match_date", str(upcoming_date))
            .execute()
        )
        prev_row = prev_resp.data[0] if prev_resp.data else None
        prev_status = prev_row.get("status") if prev_row else None
        prev_updated_at = prev_row.get("updated_at") if prev_row else None

        now = datetime.now()
        now_iso = now.isoformat()

        # 2. Check 15-Minute Grace Period
        is_grace_period = False
        if prev_updated_at and prev_status == "IN":
            try:
                prev_time = datetime.fromisoformat(
                    prev_updated_at.replace("Z", "+00:00")
                )
                now_cmp = (
                    datetime.now(prev_time.tzinfo) if prev_time.tzinfo else now
                )
                minutes_elapsed = (now_cmp - prev_time).total_seconds() / 60
                if minutes_elapsed <= 15:
                    is_grace_period = True
            except Exception as e:
                print(f"Timestamp parse error: {e}")

        # 3. Upsert current status
        supabase.table("availability").upsert(
            {
                "player_id": str(player_id),
                "match_date": str(upcoming_date),
                "status": status,
                "updated_at": now_iso,
            },
            on_conflict="player_id, match_date",
        ).execute()

        # 4. Flake Logging: Only flag if outside 15-min grace window
        if prev_status == "IN" and status == "OUT" and not is_grace_period:
            match_datetime = datetime.combine(upcoming_date, time(19, 0))
            hours_until = (match_datetime - now).total_seconds() / 3600
            is_late = hours_until <= 24

            if is_late:
                supabase.table("flake_logs").insert(
                    {
                        "player_id": str(player_id),
                        "match_date": str(upcoming_date),
                        "hours_before_match": round(hours_until, 1),
                        "is_late": True,
                    }
                ).execute()

        # 5. Auto-bump Logic (Cap at 12)
        if status == "IN":
            in_avail = (
                supabase.table("availability")
                .select("player_id, updated_at, players(is_core, name)")
                .eq("match_date", str(upcoming_date))
                .eq("status", "IN")
                .execute()
            ).data or []

            if len(in_avail) > 12:
                ringers = [
                    a for a in in_avail if not a["players"].get("is_core")
                ]
                if ringers:
                    ringers.sort(
                        key=lambda x: x.get("updated_at") or "", reverse=True
                    )
                    bump_target = ringers[0]
                else:
                    in_avail.sort(
                        key=lambda x: x.get("updated_at") or "", reverse=True
                    )
                    bump_target = in_avail[0]

                supabase.table("availability").update(
                    {"status": "OUT"}
                ).eq("player_id", bump_target["player_id"]).eq(
                    "match_date", str(upcoming_date)
                ).execute()

    return redirect("/rsvp")


@app.route("/add-player-rsvp", methods=["POST"])
def add_player_rsvp():
    name = request.form.get("name", "").strip()
    self_rating = request.form.get("rating", "3")
    upcoming_date = get_upcoming_monday()

    if name:
        base_elo = ELO_MAP.get(self_rating, 1000)
        new_p = (
            supabase.table("players")
            .insert({"name": name, "elo_rating": base_elo, "is_core": False})
            .execute()
        )
        if new_p.data:
            p_id = new_p.data[0]["id"]
            supabase.table("availability").upsert(
                {
                    "player_id": str(p_id),
                    "match_date": str(upcoming_date),
                    "status": "IN",
                    "updated_at": datetime.now().isoformat(),
                },
                on_conflict="player_id, match_date",
            ).execute()
            return redirect(url_for("rsvp_page", new_id=p_id, new_name=name))

    return redirect(url_for("rsvp_page"))


@app.route("/finances")
@admin_required
def finances():
    players_res = (
        supabase.table("players").select("*").order("name").execute().data or []
    )
    matches_res = (
        supabase.table("matches").select("*").execute().data or []
    )
    total_matches = len(matches_res)

    attendance_res = (
        supabase.table("match_attendance").select("player_id").execute().data
        or []
    )
    appearance_counts = {}
    for record in attendance_res:
        p_id = record["player_id"]
        appearance_counts[p_id] = appearance_counts.get(p_id, 0) + 1

    flakes_res = (
        supabase.table("flake_logs")
        .select("player_id")
        .eq("is_late", True)
        .execute()
        .data
        or []
    )
    flake_counts = {}
    for record in flakes_res:
        p_id = record["player_id"]
        flake_counts[p_id] = flake_counts.get(p_id, 0) + 1

    # Tally MVP totals
    mvp_counts = {}
    for m in matches_res:
        mid = m.get("mvp_player_id")
        if mid:
            mvp_counts[mid] = mvp_counts.get(mid, 0) + 1

    core_players, ringers = [], []
    for p in players_res:
        p["appearances"] = appearance_counts.get(p["id"], 0)
        p["late_flakes"] = flake_counts.get(p["id"], 0)
        p["mvp_count"] = mvp_counts.get(p["id"], 0)
        p["turnout_pct"] = (
            round((p["appearances"] / total_matches * 100), 1)
            if total_matches > 0
            else 0
        )
        if p.get("is_core"):
            core_players.append(p)
        else:
            ringers.append(p)

    ringers.sort(key=lambda x: x["appearances"], reverse=True)
    core_players.sort(key=lambda x: x["name"])

    return render_template(
        "finances.html",
        core_players=core_players,
        ringers=ringers,
        total_matches=total_matches,
    )


@app.route("/toggle-core/<player_id>", methods=["POST"])
@admin_required
def toggle_core(player_id):
    p = (
        supabase.table("players")
        .select("is_core")
        .eq("id", player_id)
        .execute()
        .data[0]
    )
    supabase.table("players").update({"is_core": not p["is_core"]}).eq(
        "id", player_id
    ).execute()
    return redirect(url_for("finances"))


@app.route("/vote-mvp", methods=["POST"])
def vote_mvp():
    match_id = request.form.get("match_id")
    voter_id = request.form.get("voter_id")
    voted_for_id = request.form.get("voted_for_id")
    next_week_status = request.form.get("next_week_status")

    if not match_id or not voter_id or not voted_for_id:
        flash("Please select both your name and your vote.")
        return redirect(url_for("rsvp_page"))

    if voter_id == voted_for_id:
        flash("You cannot vote for yourself! 😉")
        return redirect(url_for("rsvp_page"))

    # 1. Bank the Star Player vote
    supabase.table("mvp_votes").upsert(
        {
            "match_id": match_id,
            "voter_id": voter_id,
            "voted_for_id": voted_for_id,
        },
        on_conflict="match_id, voter_id",
    ).execute()

    # 2. Early RSVP Hook: Auto-commit their availability for next Monday
    if next_week_status in ["IN", "OUT"]:
        upcoming_date = get_upcoming_monday()
        supabase.table("availability").upsert(
            {
                "player_id": str(voter_id),
                "match_date": str(upcoming_date),
                "status": next_week_status,
                "updated_at": datetime.now().isoformat(),
            },
            on_conflict="player_id, match_date",
        ).execute()
        flash("Vote cast & you're locked in for next week! ⭐⚽")
    else:
        flash("Vote recorded! ⭐")

    return redirect(url_for("rsvp_page"))

@app.route("/finalize-mvp", methods=["POST"])
@admin_required
def finalize_mvp():
    match_id = request.form.get("match_id")
    winner_id = request.form.get("winner_id")

    if not match_id or not winner_id:
        return redirect(url_for("index"))

    match_res = (
        supabase.table("matches")
        .select("mvp_player_id")
        .eq("id", match_id)
        .execute()
    )
    prev_mvp_id = (
        match_res.data[0].get("mvp_player_id") if match_res.data else None
    )

    # If re-awarding to someone else, revert previous winner's +20 Elo
    if prev_mvp_id and prev_mvp_id != winner_id:
        prev_p = (
            supabase.table("players")
            .select("elo_rating")
            .eq("id", prev_mvp_id)
            .execute()
        )
        if prev_p.data:
            supabase.table("players").update(
                {"elo_rating": prev_p.data[0]["elo_rating"] - 20}
            ).eq("id", prev_mvp_id).execute()

    # Award +20 Elo to new winner (if not already awarded)
    if prev_mvp_id != winner_id:
        new_p = (
            supabase.table("players")
            .select("name, elo_rating")
            .eq("id", winner_id)
            .execute()
        )
        if new_p.data:
            supabase.table("players").update(
                {"elo_rating": new_p.data[0]["elo_rating"] + 20}
            ).eq("id", winner_id).execute()
            flash(
                f"⭐ {new_p.data[0]['name']} confirmed as Star Player (+20 Elo)! 🏆"
            )

    supabase.table("matches").update({"mvp_player_id": winner_id}).eq(
        "id", match_id
    ).execute()
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)