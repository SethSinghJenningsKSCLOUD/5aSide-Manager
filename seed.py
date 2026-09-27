import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)

# Initial sample player pool
sample_players = [
    {"name": "Seth", "is_core": True, "elo_rating": 1100.0},
    {"name": "Rob", "is_core": True, "elo_rating": 1150.0},
    {"name": "Huxley", "is_core": True, "elo_rating": 1100.0},
    {"name": "Michael", "is_core": True, "elo_rating": 1000.0},
    {"name": "Bea", "is_core": True, "elo_rating": 900.0},
    {"name": "Evie", "is_core": True, "elo_rating": 850.0},
    {"name": "Tom", "is_core": True, "elo_rating": 1000.0},
    {"name": "Sandy", "is_core": True, "elo_rating": 1200.0},
    {"name": "Gabriele", "is_core": True, "elo_rating": 950.0},
    {"name": "Samuel", "is_core": True, "elo_rating": 950.0},
    {"name": "Bridget", "is_core": True, "elo_rating": 700.0},
    {"name": "Conor", "is_core": True, "elo_rating": 1050.0},
    {"name": "Finn", "is_core": True, "elo_rating": 1150.0},
    {"name": "Ollie", "is_core": True, "elo_rating": 1050.0},
    {"name": "Tyga", "is_core": True, "elo_rating": 850.0},
    {"name": "Matthew (Ringer)", "is_core": False, "elo_rating": 850.0},
    {"name": "Simon (Ringer)", "is_core": False, "elo_rating": 1100},
]

try:
    data, count = supabase.table("players").upsert(sample_players, on_conflict="name").execute()
    print("Successfully seeded players table!")
except Exception as e:
    print(f"Error seeding database: {e}")