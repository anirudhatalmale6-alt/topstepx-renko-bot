TopstepX Renko Multi-TF Alignment Bot
======================================

Same strategy as your Pine Script but executes directly via TopstepX API.
No TradingView, no TradersPost - direct execution for minimal latency.

SETUP
-----
1. Install Python 3.10+ from python.org

2. Open Command Prompt (Windows) or Terminal (Mac) and run:
   pip install project-x-py pytz

3. Set your credentials (replace with your actual values):

   Windows:
     set PROJECT_X_USERNAME=your_topstepx_email
     set PROJECT_X_API_KEY=your_api_key

   Mac/Linux:
     export PROJECT_X_USERNAME=your_topstepx_email
     export PROJECT_X_API_KEY=your_api_key

RUNNING
-------
For NQ with brick size 3:
   python bot.py --symbol NQ --brick-size 3 --qty 1

For ES with brick size 1:
   python bot.py --symbol ES --brick-size 1 --qty 1

WHAT IT DOES
------------
- Connects to TopstepX WebSocket for real-time price data
- Builds Renko bricks for 1m, 3m, 5m, 15m timeframes
- Enters LONG when all 4 timeframes show bullish bricks
- Enters SHORT when all 4 timeframes show bearish bricks
- Exits when any timeframe breaks alignment
- Only trades during session (9:27 AM - 4:00 PM ET)
- Auto-flattens all positions at session end
- Auto-flattens on shutdown (Ctrl+C)

STOPPING
--------
Press Ctrl+C. The bot will flatten any open position before exiting.

IMPORTANT
---------
- TopstepX requires the bot to run on YOUR device (no VPS/VPN)
- The bot places real orders - test on a practice account first
- Brick size must match what you use on TradingView
