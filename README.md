# Steve's Picks

NFL paper betting on **real Kalshi prices**, with live ESPN scores and play-by-play.
No real money, no Kalshi account and no API key needed.

## What it does
- **Games**: live, upcoming and final NFL games with ESPN scores, and the Kalshi win price for each team
- **Game page**: every open Kalshi market for that game (winner first, then spread, total and player props), with Yes/No prices in cents, plus play-by-play and a box score
- **This Week**: recommended picks with their chance of winning, and a pick sheet with moneyline, spread and total for every game
- **Bet slip**: bet in dollars or contracts with Kalshi's fee; add 2+ picks to make a combo (parlay)
- **My Bets**: open and settled bets; singles can be sold early at the current price, like on Kalshi
- **Record**: balance, profit/loss, W-L, ROI, streak, results by week and by bet type
- Bets settle on their own when Kalshi settles the market (checked every minute)

## Try it on your PC (Windows)
1. Install Python 3 from python.org (tick "Add Python to PATH").
2. Double-click **run.bat**. Your browser opens to http://localhost:8080
   - **run-sample-data.bat** runs with fake sample data, handy for a first look.

## Put it on Railway
1. Create a new project, then **Deploy from GitHub repo** (push this folder to a repo) or use `railway up` from this folder.
2. Railway detects Python and runs `python server.py` (see Procfile).
3. **Keep the record between deploys**: add a **Volume** to the service mounted at `/data`, then add the variable `DATA_DIR=/data`. Without a volume, bets reset on every redeploy.
4. Optional variables:
   - `APP_PIN=1234` makes the app ask for a PIN before placing bets, so only Steve can bet on his record
   - `START_BANK=1000` sets the starting paper balance
5. Under **Settings → Networking**, click **Generate Domain** to get Steve's link.

## Notes
- Prices come from Kalshi's public market data. Combo prices are the legs multiplied together; Kalshi's own combo quote can differ a little, especially for picks from the same game.
- Recommended picks use Kalshi's own prices as the chance of winning. They are the likeliest picks, not guaranteed winners.
- If Kalshi hasn't opened a market yet, the app shows "No line yet" or "no price" instead of a fake number.
