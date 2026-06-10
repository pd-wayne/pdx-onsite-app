PDX ONSITE — PICKUP STATION
===========================
Version 1.0 | PhotoDay PDX Integration

WHAT THIS APP DOES
------------------
Polls the PDX API for pickup orders, displays them in a live queue,
and lets staff confirm pickups by scanning or typing the order barcode.
No PDX Agent required — this app replaces it entirely.

REQUIREMENTS
------------
- Windows 10 or 11
- Python 3.10+ (for running from source)
- Internet connection (to reach api.photoday.io)
- Epson TM-T20III receipt printer (USB, named correctly in Windows)
- SumatraPDF installed OR SumatraPDF.exe placed in this folder

FIRST TIME SETUP
----------------
1. Double-click SETUP.bat (installs Python dependencies)
2. Place SumatraPDF.exe in this folder if not installed system-wide
3. Double-click RUN.bat to start the app
4. Click Settings (bottom-left) and enter:
   - Lab ID (from PDX admin panel)
   - API Key (from PDX admin panel)
   - Printer name (auto-detected from Windows)
   - Studio name
5. Click "Test Connection" to verify credentials
6. Click "Save Settings" — polling starts automatically

DAILY USE
---------
- Live Queue shows all received pickup orders
- Orders turn amber after 15 min, red after 30 min (configurable)
- Confirm a pickup by:
  a) Clicking "Confirm Pickup" on the order card, OR
  b) Going to "Scan to Confirm" and scanning the receipt barcode
- Confirmed orders move to Order History

COMPILING TO .EXE
-----------------
To distribute as a standalone .exe (no Python required on target machine):
1. Run SETUP.bat first to install dependencies
2. Double-click BUILD_EXE.bat
3. Output will be in the dist/ folder: "PDX Onsite.exe"
Note: The .exe will be ~100MB due to bundled Python runtime.

FILES
-----
main.py          Entry point
bridge.py        Python↔JS API bridge
api.py           PDX API calls
db.py            SQLite order database
poller.py        Background polling thread
printer.py       Receipt PDF generation + printing
config.py        Settings load/save
src/index.html   Frontend UI

pdx_onsite.db    Created on first run (order database)
pdx_onsite_config.json  Created when you save settings

LOGS
----
pdx_onsite.log — check this file if anything isn't working

SUPPORT
-------
For PDX API issues, contact the PDX team.
Carrier payload for pickup: {"carrier":"Pickup","trackingNumber":""}
