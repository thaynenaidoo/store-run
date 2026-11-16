# Store Run

A small, self-hosted household shopping app. The original `food.csv` and `household.csv` files stay in place and are imported the first time the app starts with an empty database. The importer detects tab-separated data, creates catalog entries from forecast quantities/prices, and keeps dated purchases separate from legacy month-only history. It does not invent a year for the old month columns.

## Run locally

Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open <http://localhost:8000>. The first screen creates the household owner account; passwords must be at least 10 characters. From the owner account, use **Members** to create separate logins for other people. Everyone sees the same catalog, shopping list, and purchase history.

The app stores its SQLite database and signing key in `instance/` by default. Keep that folder private and backed up. Set `DATA_DIR` to move it elsewhere.

## Proxmox LXC

A small Debian or Ubuntu LXC on your LAN is enough; Docker is not required. The service listens on port `8000`. Copy this folder, including both CSVs, to `/opt/store-run` in the container before the first start.

Inside the LXC, install Python and create the service account and persistent data directory:

```sh
apt update
apt install -y python3 python3-venv
useradd --system --home /var/lib/store-run --shell /usr/sbin/nologin store-run
install -d -o store-run -g store-run /var/lib/store-run
cd /opt/store-run
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `/etc/store-run.env`:

```sh
DATA_DIR=/var/lib/store-run
COOKIE_SECURE=0
```

Install the service file and start it:

```sh
cp /opt/store-run/deploy/store-run.service.example /etc/systemd/system/store-run.service
systemctl daemon-reload
systemctl enable --now store-run
systemctl status store-run
```

Open `http://<LXC-IP>:8000` from devices on your home network. On first startup, the CSVs are imported into `/var/lib/store-run/household.db`; after that, the database is the live source of truth. Back up both `household.db` and `secret_key` from the data directory. Stop the service before copying those files for a simple consistent backup.

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

- **Store lists** are standing lists grouped from the imported CSV catalog. Source sections roll up into Beverages, Food, and Household; more specific food sections such as Protein and Pantry remain labeled on each item. Choose Pak’n Save, Chemist Warehouse, or another store to see usual quantities, saved unit prices, and category/store estimates. Editing a usual quantity updates the shared catalog; use the pencil action to update its price or other details.
- **Update prices** uses the selected retailer’s saved product pages. Pak’n Save can also use Grocer’s public product catalog and price file, fixed to PAK’nSAVE Highland Park; there is no location selector. If Chemist Warehouse asks for a pickup location, use Botany, Auckland.
- Item refresh prefers structured NZD price metadata from its saved retailer product page, then falls back to the matched Grocer product at Highland Park. Retailer lookup is configured for Pak’n Save, Chemist Warehouse, Woolworths, New World, The Warehouse, Nespresso, and Bostock’s Organic. Store-list search shortcuts open each retailer’s own product search where available. If a retailer blocks automated price reads or has no saved product link, open its page from the item row and save the exact link in Edit item. To confirm a Grocer product, search by name/size, select the exact product and save; its ID and direct Grocer page are stored for later refreshes. Prices are indicative, may vary, and sale prices are preferred over regular prices; club-only prices are not applied.
- In **Edit item**, use **Update this item’s price** to refresh the item from its saved retailer product page first, then its matched Grocer product at Highland Park. It shows when and where that item was last refreshed.
- Store-list and active-list items open searches for Pak’n Save, Chemist Warehouse, Woolworths, and Nespresso. Bostock’s opens its shop catalog because its site ignores search query parameters. A saved product link on the matching retailer opens directly.
- **Add store list to run** copies that store’s positive-quantity items and usual quantities into the active shopping list. Zero-quantity catalog entries remain visible in the standing list but are skipped for the run.
- Use ↗ beside any list item to open its retailer product page or search. Add products to your logged-in cart on the retailer site.
- **Copy list & open PAK’nSAVE** copies the selected Pak’n Save store list, usual quantities, and estimates to the clipboard, then opens the official shop. Add the products to the logged-in retailer cart there; Pak’n Save does not expose a documented bulk-cart import, so Store Run does not modify the retailer cart directly.
- Add regulars to the catalog with their usual quantity, preferred store, current estimate, and optional product page link.
- Quantities are whole numbers; prices can still include cents. Fractional quantities already stored are rounded to the nearest whole number at startup. Build the shopping list from the catalog and adjust this run's quantity with the plus/minus controls; it won't overwrite the usual quantity.
- Mark a list item bought to add a dated purchase at its saved price. Edit the catalog price when prices change, or change the price before marking an item bought.
- The overview shows the current list estimate, this month's dated spend, a six-month chart, and recent purchases. Imported legacy purchases remain labeled by month and source without a guessed year.
- **History** reports dated spending over the last 12 calendar months, monthly totals, food/beverage versus household spend, spend by store, and highlights such as the largest month, top store, largest purchase, repeat item, and active shopping months. Open any month to review its purchases, correct a purchase's unit price, or remove a purchase. Manual monthly spend remains labeled as not itemized. Imported CSV purchases without a year remain separately labeled and are not assigned to a month in this report.
- Use **Export** for a CSV copy of purchase history.

Retailer checkout is intentionally left to each retailer's own site. Product links are shortcuts, not automated orders.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
```
