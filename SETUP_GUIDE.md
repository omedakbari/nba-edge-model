# Environment Setup Guide — From Zero to Running Code

This guide assumes you're on macOS (adjust for Windows/Linux where noted).

---

## Step 1: Install Core Tools

### 1a. Install Homebrew (macOS package manager)
Open Terminal (Cmd + Space → type "Terminal") and paste:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Follow the prompts. When done, close and reopen Terminal.

### 1b. Install Python 3.11+
```bash
brew install python@3.12
```
Verify:
```bash
python3 --version
# Should show Python 3.12.x
```

### 1c. Install VS Code
Download from: https://code.visualstudio.com/
Install these extensions (click Extensions icon in left sidebar, search for each):
- **Python** (by Microsoft) — essential
- **Jupyter** (by Microsoft) — for notebooks
- **SQLite Viewer** — inspect your database visually
- **GitLens** — enhanced Git integration

### 1d. Install Git
```bash
brew install git
```
Configure it:
```bash
git config --global user.name "Your Name"
git config --global user.email "your.email@berkeley.edu"
```

---

## Step 2: Create the Project

### 2a. Create project directory
```bash
mkdir -p ~/projects/nba-edge-model
cd ~/projects/nba-edge-model
```

### 2b. Initialize Git
```bash
git init
```

### 2c. Create virtual environment
A virtual environment keeps this project's packages isolated from everything else:
```bash
python3 -m venv venv
```

### 2d. Activate it
```bash
source venv/bin/activate
```
You should see `(venv)` at the start of your terminal prompt. **You need to activate this every time you open a new terminal to work on this project.**

Pro tip: Add this alias to your `~/.zshrc` so you can just type `nba`:
```bash
echo 'alias nba="cd ~/projects/nba-edge-model && source venv/bin/activate"' >> ~/.zshrc
source ~/.zshrc
```

### 2e. Install dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```
(We'll create requirements.txt in the next step)

---

## Step 3: Create Project Structure

Run this from the project root (`~/projects/nba-edge-model`):
```bash
# Create directories
mkdir -p config
mkdir -p data/raw data/processed
mkdir -p src/ingestion src/features src/models src/evaluation src/utils
mkdir -p notebooks
mkdir -p dashboard/components
mkdir -p scripts
mkdir -p tests

# Create __init__.py files (makes Python treat folders as packages)
touch src/__init__.py
touch src/ingestion/__init__.py
touch src/features/__init__.py
touch src/models/__init__.py
touch src/evaluation/__init__.py
touch src/utils/__init__.py

# Create placeholder files
touch config/settings.py
touch scripts/pull_season_data.py
touch scripts/daily_update.py
```

---

## Step 4: Open in VS Code

```bash
code .
```
This opens the entire project in VS Code. You should see the folder structure in the left sidebar.

---

## Step 5: Create a GitHub Repo

1. Go to https://github.com/new
2. Repository name: `nba-edge-model`
3. Description: "Quantitative NBA prediction model — player props, spreads, and totals"
4. Keep it **Private** for now (you can make it public when it's polished)
5. Do NOT initialize with README (we'll push our local repo)

Then in terminal:
```bash
git remote add origin https://github.com/YOUR_USERNAME/nba-edge-model.git
git add .
git commit -m "initial project scaffold"
git branch -M main
git push -u origin main
```

---

## Step 6: Get Your API Key (for odds data later)

1. Go to https://the-odds-api.com/
2. Sign up for free tier
3. Copy your API key
4. Create a `.env` file in your project root:
```
ODDS_API_KEY=your_key_here
```

**IMPORTANT:** This file is gitignored. Never commit API keys.

---

## Step 7: Verify Everything Works

Open VS Code, create a new file called `test_setup.py` in the project root:
```python
"""Quick sanity check that the environment is working."""

import pandas as pd
import numpy as np
from nba_api.stats.endpoints import playergamelog
from nba_api.stats.static import players

# Find LeBron
lebron = [p for p in players.get_players() if p['full_name'] == 'LeBron James'][0]
print(f"Found: {lebron['full_name']} (ID: {lebron['id']})")

# Pull his last 10 games
gamelog = playergamelog.PlayerGameLog(
    player_id=lebron['id'],
    season='2024-25',
    season_type_all_star='Regular Season'
)
df = gamelog.get_data_frames()[0].head(10)

print(f"\nLast 10 games:\n")
print(df[['GAME_DATE', 'MATCHUP', 'MIN', 'PTS', 'REB', 'AST']].to_string(index=False))
print(f"\nAverages — PTS: {df['PTS'].mean():.1f}, REB: {df['REB'].mean():.1f}, AST: {df['AST'].mean():.1f}")
print("\n✅ Environment is working!")
```

Run it:
```bash
python test_setup.py
```

If you see LeBron's stats print out, you're good to go.

---

## Troubleshooting

**"python3: command not found"**
→ Restart terminal after installing Python. If still broken: `brew link python@3.12`

**"No module named 'nba_api'"**
→ Make sure your venv is activated (`source venv/bin/activate`), then `pip install nba_api`

**nba_api returns empty data or times out**
→ NBA.com rate limits aggressively. Add `time.sleep(0.6)` between API calls. We handle this in the pipeline code.

**VS Code not using the right Python**
→ Cmd+Shift+P → "Python: Select Interpreter" → Choose the one inside your `venv/` folder
