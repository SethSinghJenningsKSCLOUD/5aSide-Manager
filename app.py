from datetime import date, datetime, time, timedelta
import os
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from supabase import Client, create_client
from team_balancer import generate_balanced_teams, update_elo_ratings

load_dotenv()

app = Flask(__name__)

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)

ELO_MAP = {"1": 700, "2": 850, "3": 1000, "4": 1150, "5": 1300}


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
def index():
    upcoming_date = get_upcoming_monday()
    response = supabase.table("players").select("*").order("name").execute()
    players = response.data or []

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

    return render_template(
        "index.html",
        players=players,
        match_result=None,
        late_alerts=late_alerts,
        upcoming_date=upcoming_date,
    )


@app.route("/sync-attendance", methods=["POST"])
def sync_attendance():
    selected_ids = request.form.getlist("available_players")
    upcoming_date = get_upcoming_monday()
    sync_admin_attendance(selected_ids, upcoming_date)
    return redirect(url_for("index"))


@app.route("/generate", methods=["POST"])
def generate():
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


@app.route("/record-score", methods=["POST"])
def record_score():
    team_a_ids = request.form.getlist("team_a_ids")
    team_b_ids = request.form.getlist("team_b_ids")
    score_a = int(request.form.get("score_a", 0))
    score_b = int(request.form.get("score_b", 0))
    short_team = request.form.get("short_team", "none")
    minutes_short = int(request.form.get("minutes_short", 0))

    mvp_id = request.form.get("mvp_id") or None
    top_scorer_id = request.form.get("top_scorer_id") or None

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
        top_scorer_id=top_scorer_id,
    )

    for player_id, new_elo in rating_updates.items():
        supabase.table("players").update({"elo_rating": new_elo}).eq(
            "id", player_id
        ).execute()

    today = date.today().isoformat()
    match_data = {
        "match_date": today,
        "team_a_score": score_a,
        "team_b_score": score_b,
        "is_completed": True,
        "short_team": short_team,
        "minutes_short": minutes_short,
        "mvp_player_id": mvp_id,
        "top_scorer_id": top_scorer_id,
    }

    match_res = (
        supabase.table("matches")
        .upsert(match_data, on_conflict="match_date")
        .execute()
    )
    match_id = match_res.data[0]["id"]

    supabase.table("match_attendance").delete().eq(
        "match_id", match_id
    ).execute()
    attendance_records = [
        {"match_id": match_id, "player_id": p_id, "team_assigned": "A"}
        for p_id in team_a_ids
    ] + [
        {"match_id": match_id, "player_id": p_id, "team_assigned": "B"}
        for p_id in team_b_ids
    ]

    supabase.table("match_attendance").insert(attendance_records).execute()
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

    return render_template(
        "rsvp.html", players=players, rsvps=rsvps, match_date=upcoming_date
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


if __name__ == "__main__":
    app.run(debug=True)