import itertools

def generate_balanced_teams(players: dict, handicap: float = 1.20):
    """
    players: dict of {'Name': Elo_Rating}
    handicap: boost applied to a 4-man team facing a 5-man team
    """
    names = list(players.keys())
    total_players = len(names)
    
    if total_players < 2:
        return None

    # Calculate team sizes (5v5 for 10, 5v4 for 9)
    size_a = (total_players + 1) // 2
    size_b = total_players // 2
    
    best_diff = float('inf')
    best_matchup = None

    for team_a in itertools.combinations(names, size_a):
        team_b = [p for p in names if p not in team_a]
        
        sum_a = sum(players[p] for p in team_a)
        sum_b = sum(players[p] for p in team_b)
        
        # Apply handicap if teams are 5 vs 4
        if size_a != size_b:
            effective_b = sum_b * handicap
            diff = abs(sum_a - effective_b)
        else:
            diff = abs(sum_a - sum_b)
            
        if diff < best_diff:
            best_diff = diff
            best_matchup = {
                'team_a': list(team_a),
                'team_a_rating': round(sum_a, 1),
                'team_b': list(team_b),
                'team_b_rating': round(sum_b, 1),
                'diff': round(diff, 1)
            }
            
    return best_matchup
def update_elo_ratings(
    team_a_players: list, 
    team_b_players: list, 
    score_a: int, 
    score_b: int, 
    short_team: str = "none",  # "none", "team_a", or "team_b"
    minutes_short: int = 0,
    k_factor: int = 32
):
    avg_a = sum(p['elo_rating'] for p in team_a_players) / len(team_a_players)
    avg_b = sum(p['elo_rating'] for p in team_b_players) / len(team_b_players)

    # Base expected ratings
    # 0.20 weight = playing 1 man down for 60 mins shifts expected win probability by 20%
    handicap_weight = (minutes_short / 60.0) * 0.20

    if short_team == "team_a":
        # Team A was short, so lower expectations for Team A (making a win/close loss worth more)
        expected_a = 1 / (1 + 10 ** ((avg_b - avg_a) / 400)) - handicap_weight
        expected_a = max(0.05, expected_a) # Prevent negative probabilities
        expected_b = 1.0 - expected_a
    elif short_team == "team_b":
        expected_b = 1 / (1 + 10 ** ((avg_a - avg_b) / 400)) - handicap_weight
        expected_b = max(0.05, expected_b)
        expected_a = 1.0 - expected_b
    else:
        expected_a = 1 / (1 + 10 ** ((avg_b - avg_a) / 400))
        expected_b = 1.0 - expected_a

    # Determine actual outcome
    if score_a > score_b:
        actual_a, actual_b = 1.0, 0.0
    elif score_b > score_a:
        actual_a, actual_b = 0.0, 1.0
    else:
        actual_a, actual_b = 0.5, 0.5

    # Calculate point updates
    updates = {}
    for p in team_a_players:
        updates[p['id']] = round(p['elo_rating'] + k_factor * (actual_a - expected_a), 1)
    for p in team_b_players:
        updates[p['id']] = round(p['elo_rating'] + k_factor * (actual_b - expected_b), 1)

    return updates