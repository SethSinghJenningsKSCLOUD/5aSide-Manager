from itertools import combinations
import math


def generate_balanced_teams(player_pool: dict):
    """Generates two balanced teams.

    If player count is odd (e.g. 5 v 6), the smaller team receives a 1.18x
    effective strength multiplier to compensate for the numerical disadvantage.
    """
    player_names = list(player_pool.keys())
    total_players = len(player_names)

    if total_players < 2:
        return None

    size_a = total_players // 2
    size_b = total_players - size_a
    is_uneven = size_a != size_b

    best_split = None
    min_rating_diff = float("inf")

    # Evaluate all unique team splits
    for team_a_names in combinations(player_names, size_a):
        team_a = list(team_a_names)
        team_b = [p for p in player_names if p not in team_a]

        rating_a = sum(player_pool[p] for p in team_a)
        rating_b = sum(player_pool[p] for p in team_b)

        if is_uneven:
            # Team A is 5 players, Team B is 6 players.
            # Team A must have ~18% higher cumulative quality to offset the physical extra man.
            effective_rating_a = rating_a * 1.18
            effective_rating_b = rating_b
            diff = abs(effective_rating_a - effective_rating_b)
        else:
            diff = abs(rating_a - rating_b)

        if diff < min_rating_diff:
            min_rating_diff = diff
            best_split = (team_a, team_b, rating_a, rating_b)

    team_a, team_b, final_rating_a, final_rating_b = best_split

    return {
        "team_a": team_a,
        "team_b": team_b,
        "team_a_rating": round(final_rating_a / len(team_a)),
        "team_b_rating": round(final_rating_b / len(team_b)),
        "team_a_total": final_rating_a,
        "team_b_total": final_rating_b,
        "rating_diff": round(abs(final_rating_a - final_rating_b)),
    }


def update_elo_ratings(
    team_a_players: list,
    team_b_players: list,
    score_a: int,
    score_b: int,
    short_team: str = "none",
    minutes_short: int = 0,
    mvp_id: str = None,
    top_scorer_ids: list = None,
    top_scorer_id: str = None,
):
    """Calculates updated Elo ratings based on outcome, goal margins,
    player-short handicaps, and individual performance bonuses.
    """
    # Normalise top_scorer_ids so it supports lists, single IDs, or None
    if top_scorer_ids is None:
        top_scorer_ids = [top_scorer_id] if top_scorer_id else []
    elif isinstance(top_scorer_ids, str):
        top_scorer_ids = [top_scorer_ids]

    scorer_set = {str(pid) for pid in top_scorer_ids if pid}

    avg_elo_a = sum(p["elo_rating"] for p in team_a_players) / len(team_a_players)
    avg_elo_b = sum(p["elo_rating"] for p in team_b_players) / len(team_b_players)

    # 1. Expected match outcome (Logistic curve)
    expected_a = 1 / (1 + 10 ** ((avg_elo_b - avg_elo_a) / 400))
    expected_b = 1 - expected_a

    # 2. Actual outcome
    if score_a > score_b:
        actual_a, actual_b = 1.0, 0.0
    elif score_b > score_a:
        actual_a, actual_b = 0.0, 1.0
    else:
        actual_a, actual_b = 0.5, 0.5

    # 3. Goal Margin Multiplier (blowouts shift Elo faster than 1-goal wins)
    goal_diff = abs(score_a - score_b)
    margin_mult = (
        math.log(goal_diff + 1, 2) if goal_diff > 1 else 1.0
    )  # e.g., diff of 10 goals -> 3.45x

    base_k = 30
    delta_a = base_k * margin_mult * (actual_a - expected_a)
    delta_b = base_k * margin_mult * (actual_b - expected_b)

    # 4. Short-handed handicap adjustment during the game
    if short_team == "A" and minutes_short > 0:
        handicap = (minutes_short / 60) * 0.4
        delta_a *= 1.0 - handicap if delta_a < 0 else 1.0 + handicap
    elif short_team == "B" and minutes_short > 0:
        handicap = (minutes_short / 60) * 0.4
        delta_b *= 1.0 - handicap if delta_b < 0 else 1.0 + handicap

    rating_updates = {}

    for p in team_a_players:
        new_rating = round(p["elo_rating"] + delta_a)
        if mvp_id and str(p["id"]) == str(mvp_id):
            new_rating += 20  # +20 Elo MVP Bonus
        if str(p["id"]) in scorer_set:
            new_rating += 10  # +10 Elo Goal Machine Bonus
        rating_updates[p["id"]] = max(700, new_rating)

    for p in team_b_players:
        new_rating = round(p["elo_rating"] + delta_b)
        if mvp_id and str(p["id"]) == str(mvp_id):
            new_rating += 20
        if str(p["id"]) in scorer_set:
            new_rating += 10
        rating_updates[p["id"]] = max(700, new_rating)

    return rating_updates