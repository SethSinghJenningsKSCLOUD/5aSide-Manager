import os
from datetime import date, datetime, time, timedelta
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


@app.route("/")
def index():
    upcoming_date = get_upcoming_monday()

    # Fetch all players
    response = supabase.table("players").select("*").order("name").execute()
    players = response.data or []

    # Fetch RSVPs for upcoming Monday
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

    # Fetch late dropout alerts for the upcoming match
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


@app.route("/generate", methods=["POST"])
def generate():
    selected_ids = request.form.getlist("available_players")
    if not selected_ids:
        return redirect(url_for("index"))

    response = (
        supabase.table("players").select("*").in_("id", selected_ids).execute()
    )
    selected_players = response.data

    player_pool = {p["name"]: p["elo_rating"] for p in selected_players}
    matchup = generate_balanced_teams(player_pool)
    all_players = (
        supabase.table("players").select("*").order("name").execute().data
    )

    return render_template(
        "index.html",
        players=all_players,
        match_result=matchup,
        late_alerts=[],
        upcoming_date=get_upcoming_monday(),
    )


def get_player_streaks():
    # Fetch completed matches in descending order
    matches_res = (
        supabase.table("matches")
        .select("*")
        .eq("is_completed", True)
        .order("match_date", desc=True)
        .execute()
    )
    matches = matches_res.data or []

    # Fetch attendance
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

    attendance_streaks = {}
    win_streaks = {}

    for p_id in all_p_ids:
        # 1. Attendance Streak (consecutive matches played starting from most recent)
        attn_streak = 0
        for m in matches:
            m_id = m["id"]
            m_atts = match_att_map.get(m_id, [])
            if any(a["player_id"] == p_id for a in m_atts):
                attn_streak += 1
            else:
                break
        attendance_streaks[p_id] = attn_streak

        # 2. Win Streak (consecutive matches played AND won by player)
        win_streak = 0
        for m in matches:
            m_id = m["id"]
            m_atts = match_att_map.get(m_id, [])
            p_att = next((a for a in m_atts if a["player_id"] == p_id), None)
            if not p_att:
                continue

            team = p_att["team_assigned"]
            score_a = m.get("team_a_score", 0)
            score_b = m.get("team_b_score", 0)

            won = (team == "A" and score_a > score_b) or (
                team == "B" and score_b > score_a
            )
            if won:
                win_streak += 1
            else:
                break
        win_streaks[p_id] = win_streak

    return attendance_streaks, win_streaks


@app.route("/rsvp", methods=["GET"])
def rsvp_page():
    upcoming_date = get_upcoming_monday()
    players_resp = (
        supabase.table("players").select("*").order("name").execute()
    )
    players = players_resp.data or []

    avail_resp = (
        supabase.table("availability")
        .select("*")
        .eq("match_date", str(upcoming_date))
        .execute()
    )
    rsvps = {row["player_id"]: row["status"] for row in (avail_resp.data or [])}

    # Fetch Streaks
    attn_streaks, win_streaks = get_player_streaks()

    for p in players:
        p["attn_streak"] = attn_streaks.get(p["id"], 0)
        p["win_streak"] = win_streaks.get(p["id"], 0)

    return render_template(
        "rsvp.html", players=players, rsvps=rsvps, match_date=upcoming_date
    )

@app.route("/rsvp", methods=["POST"])
def submit_rsvp():
    player_id = request.form.get("player_id")
    status = request.form.get("status")
    upcoming_date = get_upcoming_monday()

    if player_id and status in ["IN", "OUT"]:
        # Check previous RSVP status
        prev_resp = (
            supabase.table("availability")
            .select("status")
            .eq("player_id", str(player_id))
            .eq("match_date", str(upcoming_date))
            .execute()
        )
        prev_status = (
            prev_resp.data[0]["status"] if prev_resp.data else None
        )

        # Upsert new status
        supabase.table("availability").upsert(
            {
                "player_id": str(player_id),
                "match_date": str(upcoming_date),
                "status": status,
            },
            on_conflict="player_id, match_date",
        ).execute()

        # Log Flaker if switching from IN -> OUT
        if prev_status == "IN" and status == "OUT":
            match_datetime = datetime.combine(
                upcoming_date, time(19, 0)
            )  # Assumes 7 PM kickoff
            now = datetime.now()
            hours_until = (match_datetime - now).total_seconds() / 3600
            is_late = hours_until <= 24  # Flag if within 24 hrs

            supabase.table("flake_logs").insert(
                {
                    "player_id": str(player_id),
                    "match_date": str(upcoming_date),
                    "hours_before_match": round(hours_until, 1),
                    "is_late": is_late,
                }
            ).execute()

    return redirect("/rsvp")


@app.route("/add-player-rsvp", methods=["POST"])
def add_player_rsvp():
    name = request.form.get("name", "").strip()
    self_rating = request.form.get("rating", "3")
    upcoming_date = get_upcoming_monday()

    if name:
        base_elo = ELO_MAP.get(self_rating, 1000)

        # 1. Create Player
        new_p = (
            supabase.table("players")
            .insert({"name": name, "elo_rating": base_elo, "is_core": False})
            .execute()
        )
        if new_p.data:
            p_id = new_p.data[0]["id"]

            # 2. Automatically set RSVP to IN
            supabase.table("availability").upsert(
                {
                    "player_id": str(p_id),
                    "match_date": str(upcoming_date),
                    "status": "IN",
                },
                on_conflict="player_id, match_date",
            ).execute()

            # Redirect back to RSVP page and pass new player details in URL
            return redirect(url_for("rsvp_page", new_id=p_id, new_name=name))

    return redirect(url_for("rsvp_page"))


@app.route("/record-score", methods=["POST"])
def record_score():
    team_a_ids = request.form.getlist("team_a_ids")
    team_b_ids = request.form.getlist("team_b_ids")
    score_a = int(request.form.get("score_a", 0))
    score_b = int(request.form.get("score_b", 0))
    short_team = request.form.get("short_team", "none")
    minutes_short = int(request.form.get("minutes_short", 0))

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


@app.route("/finances")
def finances():
    players_res = supabase.table("players").select("*").execute()
    players = players_res.data or []

    matches_res = supabase.table("matches").select("id").execute()
    total_matches = len(matches_res.data or [])

    attendance_res = (
        supabase.table("match_attendance").select("player_id").execute()
    )
    appearance_counts = {}
    for record in attendance_res.data or []:
        p_id = record["player_id"]
        appearance_counts[p_id] = appearance_counts.get(p_id, 0) + 1

    # Fetch Late Dropouts Count (Hall of Shame)
    flakes_res = (
        supabase.table("flake_logs")
        .select("player_id")
        .eq("is_late", True)
        .execute()
    )
    flake_counts = {}
    for record in flakes_res.data or []:
        p_id = record["player_id"]
        flake_counts[p_id] = flake_counts.get(p_id, 0) + 1

    core_players = []
    ringers = []

    for p in players:
        p["appearances"] = appearance_counts.get(p["id"], 0)
        p["late_flakes"] = flake_counts.get(p["id"], 0)
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
    new_status = not p["is_core"]
    supabase.table("players").update({"is_core": new_status}).eq(
        "id", player_id
    ).execute()
    return redirect(url_for("finances"))


if __name__ == "__main__":
    app.run(debug=True)