import os
from datetime import date
from flask import Flask, render_template, request, redirect, url_for
from supabase import create_client, Client
from dotenv import load_dotenv
from team_balancer import generate_balanced_teams, update_elo_ratings

load_dotenv()

app = Flask(__name__)

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)

@app.route("/")
def index():
    # Fetch all players from Supabase, sorted by name
    response = supabase.table("players").select("*").order("name").execute()
    players = response.data
    return render_template("index.html", players=players, match_result=None)

@app.route("/generate", methods=["POST"])
def generate():
    # Get IDs of players checked in the form
    selected_ids = request.form.getlist("available_players")
    
    if not selected_ids:
        return redirect(url_for("index"))
    
    # Fetch details for selected players
    response = supabase.table("players").select("*").in_("id", selected_ids).execute()
    selected_players = response.data
    
    # Format into dict for team balancer: {'Player Name': Elo}
    player_pool = {p["name"]: p["elo_rating"] for p in selected_players}
    
    # Run algorithm
    matchup = generate_balanced_teams(player_pool)
    
    # Re-fetch all players so the list remains populated
    all_players = supabase.table("players").select("*").order("name").execute().data
    
    return render_template("index.html", players=all_players, match_result=matchup)

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

    res_a = supabase.table("players").select("*").in_("id", team_a_ids).execute()
    res_b = supabase.table("players").select("*").in_("id", team_b_ids).execute()

    # 1. Update Elo Ratings
    rating_updates = update_elo_ratings(
        res_a.data, 
        res_b.data, 
        score_a, 
        score_b, 
        short_team=short_team, 
        minutes_short=minutes_short
    )

    for player_id, new_elo in rating_updates.items():
        supabase.table("players").update({"elo_rating": new_elo}).eq("id", player_id).execute()

    # 2. Save the overall Match Data
    today = date.today().isoformat()
    
    match_data = {
        "match_date": today,
        "team_a_score": score_a,
        "team_b_score": score_b,
        "is_completed": True,
        "short_team": short_team,
        "minutes_short": minutes_short
    }
    
    match_res = supabase.table("matches").upsert(match_data, on_conflict="match_date").execute()
    match_id = match_res.data[0]['id']

    # 3. Save Player Attendance & Teams
    supabase.table("match_attendance").delete().eq("match_id", match_id).execute()
    
    attendance_records = []
    for p_id in team_a_ids:
        attendance_records.append({
            "match_id": match_id,
            "player_id": p_id,
            "team_assigned": "A"
        })
    for p_id in team_b_ids:
        attendance_records.append({
            "match_id": match_id,
            "player_id": p_id,
            "team_assigned": "B"
        })
        
    supabase.table("match_attendance").insert(attendance_records).execute()

    return redirect(url_for("index"))

@app.route("/finances")
def finances():
    # 1. Fetch all players
    players_res = supabase.table("players").select("*").execute()
    players = players_res.data

    # 2. Fetch total match count
    matches_res = supabase.table("matches").select("id").execute()
    total_matches = len(matches_res.data)

    # 3. Fetch all attendance logs to count appearances per player
    attendance_res = supabase.table("match_attendance").select("player_id").execute()
    
    appearance_counts = {}
    for record in attendance_res.data:
        p_id = record['player_id']
        appearance_counts[p_id] = appearance_counts.get(p_id, 0) + 1

    core_players = []
    ringers = []

    for p in players:
        p['appearances'] = appearance_counts.get(p['id'], 0)
        # Calculate percentage of total matches played
        p['turnout_pct'] = round((p['appearances'] / total_matches * 100), 1) if total_matches > 0 else 0
        
        if p.get('is_core'):
            core_players.append(p)
        else:
            ringers.append(p)

    # Sort ringers by highest appearances so frequent non-payers float to the top
    ringers.sort(key=lambda x: x['appearances'], reverse=True)
    core_players.sort(key=lambda x: x['name'])

    return render_template(
        "finances.html", 
        core_players=core_players, 
        ringers=ringers, 
        total_matches=total_matches
    )

@app.route("/toggle-core/<player_id>", methods=["POST"])
def toggle_core(player_id):
    # Fetch current status
    p = supabase.table("players").select("is_core").eq("id", player_id).execute().data[0]
    new_status = not p['is_core']
    
    # Toggle between Core Member and Ringer
    supabase.table("players").update({"is_core": new_status}).eq("id", player_id).execute()
    return redirect(url_for("finances"))

if __name__ == "__main__":
    app.run(debug=True)