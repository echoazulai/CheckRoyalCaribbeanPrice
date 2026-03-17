# Celebrity Cruises UK Price Tracker

A command-line Python tool that automatically discovers Celebrity Cruises sailings,
tracks cabin and suite pricing in GBP, and stores a full price history in a local
SQLite database so you can see how prices change over time.

> **Note:** This tool uses publicly available price information, the same data you
> would see by browsing the Celebrity Cruises website. No account or login is needed.

---

## What it does

- **Discovers** every Celebrity Cruises sailing currently available
- **Records** cabin pricing across all cabin classes (Inside, Oceanview, Balcony, Suite)
- **Flags** Retreat suite categories and guarantee (GTY) fares
- **Tracks** price changes over time — every run appends new data, nothing is deleted
- **Shows** the cheapest Retreat suite options and biggest recent price drops

---

## Setup (Windows — one-time)

### Step 1 — Install Python

If you don't have Python installed:

1. Go to <https://www.python.org/downloads/> and download the latest Python 3.x installer.
2. Run the installer. **Important:** tick the box that says **"Add Python to PATH"** before clicking Install.
3. Click **Install Now**.

### Step 2 — Download the tracker files

Copy these four files into a folder on your computer (e.g. `C:\CruiseTracker`):

```
celebrity_tracker.py
tracker_config.yaml
requirements.txt
README.md
```

### Step 3 — Install dependencies

1. Open **Command Prompt** (press `Win + R`, type `cmd`, press Enter).
2. Navigate to the folder where you saved the files:
   ```
   cd C:\CruiseTracker
   ```
3. Run:
   ```
   pip install requests pyyaml
   ```

That's it — setup is complete.

---

## Running the tracker

Open **Command Prompt**, navigate to your tracker folder, and run:

```
python celebrity_tracker.py
```

On the **first run**, the tool will:
1. Fetch all Celebrity Cruises ships and their available sailings
2. Check pricing for every sailing (this can take several minutes)
3. Print a summary showing the cheapest Retreat suites

**Subsequent runs** will add new price snapshots for comparison and show any price drops.

---

## Command options

| Command | What it does |
|---------|-------------|
| `python celebrity_tracker.py` | Full run: discover sailings + check all prices |
| `python celebrity_tracker.py --summary` | Show the current summary without fetching new data |
| `python celebrity_tracker.py --history "Apex 2026-11-15"` | Show the price history for a specific sailing |
| `python celebrity_tracker.py --currency USD` | Override the currency for this run |
| `python celebrity_tracker.py --min-nights 10` | Only track sailings of 10+ nights this run |

---

## Customising the tracker

Open `tracker_config.yaml` in Notepad and edit the settings:

```yaml
currency: "GBP"      # Change to USD, EUR, etc. if needed
country: "GB"        # Your country code
passengers: 2        # Number of adults per cabin
min_nights: 7        # Ignore sailings shorter than this
```

To track only specific ships, uncomment the `ship_filter` section:

```yaml
ship_filter:
  - "Edge"
  - "Apex"
  - "Beyond"
```

---

## Understanding the output

### Retreat Suites

Celebrity's Retreat suite categories (Sky Suite, Aqua Sky Suite, Celebrity Suite,
Royal Suite, Penthouse, Edge Villa, Iconic Suite) are flagged with `[RETREAT]`.
These include access to the exclusive Retreat sundeck, lounge, and restaurant.

### Guarantee fares (GTY)

A guarantee fare means you're booking a category class but Celebrity assigns you
a specific cabin. These often offer the best prices. Flagged as `GTY` in the output.

### Price per night

Prices shown as `£xxx/night` are calculated as:
```
price per person ÷ number of nights
```
This makes it easier to compare sailings of different lengths.

---

## Files created by the tracker

| File | Description |
|------|-------------|
| `celebrity_tracker.db` | SQLite database — all pricing history. **Don't delete this!** |
| `tracker_config.yaml` | Your configuration settings |

The database file grows over time as more price snapshots are recorded.
You can open it with a free tool like [DB Browser for SQLite](https://sqlitebrowser.org/)
if you want to explore the raw data.

---

## Scheduling automatic runs (optional)

To run the tracker automatically every day on Windows:

1. Open **Task Scheduler** (search for it in the Start menu).
2. Click **Create Basic Task**.
3. Set the trigger to **Daily** at a time that suits you.
4. For the action, set:
   - Program: `python`
   - Arguments: `C:\CruiseTracker\celebrity_tracker.py`
   - Start in: `C:\CruiseTracker`
5. Save the task.

---

## Troubleshooting

**"python is not recognised"**
Python is not on your PATH. Re-run the Python installer and tick "Add Python to PATH".

**"No Celebrity ships found"**
The Celebrity API may be temporarily unavailable. Wait a few minutes and try again.

**"API error" messages during price checks**
Some sailings may return errors — the tool logs these and continues to the next sailing.
This is normal behaviour.

**The database file is missing**
The tool creates `celebrity_tracker.db` automatically on the first run. If it's missing,
just run the tool again.

---

## Dependencies

- `requests` — HTTP library for API calls
- `pyyaml` — YAML config file parsing
- `sqlite3` — SQLite database (built into Python, no install needed)

---

*Built on top of the API patterns from [jdeath/CheckRoyalCaribbeanPrice](https://github.com/jdeath/CheckRoyalCaribbeanPrice).*
