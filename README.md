# Store Run

A self-hosted app for tracking household spending and planning store runs, with monthly breakdowns that help you see where your money goes. Credit-card CSV analysis and broader budgeting are future goals, not current features. The original `food.csv` and `household.csv` files are optional sample/legacy migration inputs, not runtime dependencies. The optional importer detects tab-separated data, creates catalog entries from forecast quantities/prices, and keeps dated purchases separate from legacy month-only history. It does not invent a year for the old month columns.

## Run locally

Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Open <http://localhost:8000>. The first screen creates the household owner account; passwords must be at least 10 characters. From the owner account, use **Members** to create separate logins for other people. Everyone sees the same catalog, shopping list, and purchase history. Python virtual environments are machine- and path-specific; create a new `.venv` after cloning or moving the project rather than committing or copying one.

The app stores its SQLite database and signing key in `instance/` by default. Keep that folder private and backed up. Set `DATA_DIR` to move it elsewhere. The CSVs in this repository are sample/legacy migration data only; a fresh install does not need them and does not import them by default. To opt in to a first-run sample import, set `IMPORT_SAMPLE_CSVS=1` before startup. Once data is in SQLite, the CSVs are no longer used.

## Proxmox LXC

A small Debian or Ubuntu LXC on your LAN is enough; Docker is not required. Clone the repository into `/opt/store-run`, then run the bootstrap script as root:

```sh
sudo git clone https://github.com/thaynenaidoo/store-run.git /opt/store-run
cd /opt/store-run
sudo ./deploy.sh
```

`deploy.sh` installs Git, Python, and venv support; creates the `store-run` service account and persistent data directory; builds a fresh virtual environment; installs requirements; creates `/etc/store-run.env` if it does not exist; installs/enables the systemd service; and checks `/healthz`. It does not overwrite an existing environment file or database. By default app code is in `/opt/store-run` and SQLite data/signing key are in `/var/lib/store-run`.

For a deployment to a custom path, set `STORE_RUN_APP_DIR` and/or `STORE_RUN_DATA_DIR` when running the script. Existing deployments can update with `sudo git -C /opt/store-run pull --ff-only && sudo /opt/store-run/deploy.sh`; the script keeps the existing data directory and environment file.

The default `/etc/store-run.env` contains:

```sh
DATA_DIR=/var/lib/store-run
COOKIE_SECURE=0
```

Open `http://<LXC-IP>:8000` from devices on your home network. The database is the live source of truth; CSV samples are not required. Back up both `household.db` and `secret_key` from the data directory. Stop the service before copying those files for a simple consistent backup.

For an existing Night Run deployment, stop its service and migrate the persistent data before starting Store Run, so the household database and signing key are preserved:

```sh
systemctl stop night-run
install -d -o store-run -g store-run /var/lib/store-run
cp -a /var/lib/night-run/. /var/lib/store-run/
chown -R store-run:store-run /var/lib/store-run
```

Update any reverse-proxy service references to `store-run.service` as part of the migration.

For remote access, prefer a VPN such as Tailscale. Do not forward port `8000` directly to the public internet. If you put the app behind an HTTPS reverse proxy, set `COOKIE_SECURE=1` in `/etc/store-run.env` and restart the service. Give the LXC a DHCP reservation or static address if you want a stable bookmark.

## Using the app

- **Store favourites** holds your reusable items, grouped into store tabs with usual quantities and prices. Grocery items are grouped into Beverages, Food, and Household; Chemist Warehouse is shown without those sections. Add, edit or remove favourites here.
- **Update prices** uses the selected retailer’s saved product pages. Pak’n Save can also use Grocer’s public product catalog and price file, fixed to PAK’nSAVE Highland Park; there is no location selector. If Chemist Warehouse asks for a pickup location, use Botany, Auckland.
- Item refresh prefers structured NZD price metadata from its saved retailer product page, then falls back to the matched Grocer product at Highland Park. Retailer lookup is configured for Pak’n Save, Chemist Warehouse, Woolworths, New World, The Warehouse, Nespresso, and Bostock’s Organic. Store-list search shortcuts open each retailer’s own product search where available. If a retailer blocks automated price reads or has no saved product link, open its page from the item row and save the exact link in Edit item. To confirm a Grocer product, search by name/size, select the exact product and save; its ID and direct Grocer page are stored for later refreshes. Prices are indicative, may vary, and sale prices are preferred over regular prices; club-only prices are not applied.
- In **Edit item**, use **Update this item’s price** to refresh the item from its saved retailer product page first, then its matched Grocer product at Highland Park. It shows when and where that item was last refreshed.
- Store-list and active-list items open searches for Pak’n Save, Chemist Warehouse, Woolworths, and Nespresso. Bostock’s opens its shop catalog because its site ignores search query parameters. A saved product link on the matching retailer opens directly.
- **Store run** is your active list for ticking off items. Add individual favourites or a whole store's positive-quantity favourites. Use **Remove store run** on a store heading to remove that store's active items, or **Clear all stores** to clear the entire run. Both ask for confirmation and preserve favourites and purchase history, even when a search filter is active.
- When items are added to the store run, Store Run checks Grocer for a cheaper price at grocery stores in the Botany/Highland Park area. Comparisons use a saved Grocer product ID or a high-confidence name match and compare against the item’s saved unit price. Availability and prices can change; unmatched items and stores Grocer cannot provide cannot be compared. A flag is advisory and never moves or reprices an item.
- Use ↗ beside any list item to open its retailer product page or search. Add products to your logged-in cart on the retailer site.
- **Copy list & open PAK’nSAVE** copies the selected Pak’n Save store list, usual quantities, and estimates to the clipboard, then opens the official shop. Add the products to the logged-in retailer cart there; Pak’n Save does not expose a documented bulk-cart import, so Store Run does not modify the retailer cart directly.
- Add regulars to the catalog with their usual quantity, preferred store, current estimate, and optional product page link.
- Quantities are whole numbers; prices can still include cents. Fractional quantities already stored are rounded to the nearest whole number at startup. Build the shopping list from the catalog and adjust this run's quantity with the plus/minus controls; it won't overwrite the usual quantity.
- Mark a list item bought to add a dated purchase at its saved price. Edit the catalog price when prices change, or change the price before marking an item bought.
- The overview shows the current list estimate, this month's dated spend, and a six-month chart. Imported legacy purchases remain labeled by month and source without a guessed year.
- **History** shows spending for this month, this calendar year to date, and the rolling 12 calendar months. Its line graph and breakdowns show known amounts from purchases and manual entries; unknown amounts are flagged. Open a month for a full-page breakdown grouped by store, with subtotals and options to add entries, correct item prices, and remove entries. Imported CSV purchases without a year remain separately labeled and are not assigned to a month in this report.
- **Add a store run** records offline or retroactive purchases for any month, grouped by Food or Household, store, and optional type (for example Food → Local Butcher → Meat). Amounts may be left blank; unknown amounts appear in the breakdown and do not inflate calculated totals. This supports local stores without product pages as well as catch-up entries such as an August run.
- Use the household-name control in the header to name the current household. The database now has a household record and household IDs on core records as multi-household groundwork; account creation and data isolation between multiple households are not enabled yet.
- Use **Export** for a CSV copy of purchase history.

Retailer checkout is intentionally left to each retailer's own site. Product links are shortcuts, not automated orders.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
```
