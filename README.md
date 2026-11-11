# AllRatesToday Exchange Rates for ERPNext

Load official exchange rates into ERPNext automatically: the RBI/FBIL reference rate, CBIC customs notification rates, Indian bank TT card rates, ECB reference rates, the table of any of 130+ central banks and tax authorities, or live market rates. Rates are written to ERPNext's own **Currency Exchange** records, so invoices, payments and journal entries pick them up with no customisation.

The official and bank-card sources are keyless: no account, no sign-up.

![Settings screenshot](docs/screenshot-settings.png)
<!-- Screenshot placeholder: AllRatesToday Settings form -->

![Currency Exchange list screenshot](docs/screenshot-currency-exchange.png)
<!-- Screenshot placeholder: Currency Exchange list filled by the app -->

## What it does

- Fetches rates once a day (live market rates: daily or hourly) and creates or updates **Currency Exchange** records: date, from currency, to currency, exchange rate, for buying, for selling.
- Uses the publisher's own date. An RBI rate published on 30 October is stored as a 30 October record, and ERPNext uses it for any later transaction until the next one arrives.
- Optionally writes separate Buying and Selling records (bank TT selling/buying rates, CBIC import/export rates).
- **Sync now** button for an immediate run, with a summary of what changed.
- **Apply ERPNext provider preset** button: fills ERPNext's **Currency Exchange Settings** so that on-demand lookups (a pair or past date with no stored record) also use AllRatesToday.
- Skips currencies that do not exist in your ERPNext Currency list and reports them, together with any currency the source does not publish, in **Last Error / Notes**. A failed sync never interrupts the scheduler.

## Which rate for which Indian rule

| Requirement | Source to pick | Notes |
| --- | --- | --- |
| Ind AS 21 / AS 11 translation, statutory books | **RBI/FBIL reference** | USD, EUR, GBP, JPY, AED, IDR against INR, published each working day |
| Customs valuation, CGST Rule 34 (rate notified by CBIC) | **CBIC customs** | Fortnightly import and export rates for 22 currencies. Turn on the Buying/Selling split to store import (Buying) and export (Selling) separately |
| Income-tax Rule 115 (SBI TT buying rate) | **Indian bank TT card**, bank = `sbi` | With the split off, the TT buying rate is written for both buying and selling |
| Bank-specific conversion (your own bank's card) | **Indian bank TT card**: `sbi`, `axis`, `bob`, `bom`, `canara`, `iob`, `union` | Indicative board rates as shown on the bank's website |
| Group reporting in EUR, or ECB-based contracts | **ECB reference** | Any base currency in the ECB table; other pairs are cross-computed inside the table |
| Another country's rule (UAE, Switzerland, UK, ...) | **Other central bank code** | Any code from the [institutions list](https://allratestoday.com/central-bank-rates-api/institutions/) |
| Treasury, pricing, intraday | **Live market rates** | Needs a free API key |

This table is guidance, not tax advice. Check which publisher your auditor or the relevant rule names.

## Installation

Self-hosted bench:

```bash
bench get-app https://github.com/AllRates-Today/allratestoday-frappe
bench --site your-site install-app allratestoday
bench --site your-site migrate
```

Frappe Cloud: add **AllRatesToday Exchange Rates** from the Marketplace to your bench, then install it on the site.

Requirements: Frappe and ERPNext v15 (v14 is expected to work), and the scheduler enabled (`bench --site your-site enable-scheduler`).

## Configuration

Open **AllRatesToday Settings** (search bar: "AllRatesToday Settings").

1. **Rate Source**: one of the sources in the table above.
2. **Bank** (bank TT card only) or **Institution Code** (other central bank only).
3. **API Key**: only for live market rates. Get a free API key at [allratestoday.com/register](https://allratestoday.com/register).
4. **Base Currency**: usually your company currency. Leave blank to use the default company's currency.
5. **Currencies**: the foreign currencies to load. Leave blank to load every currency the source publishes that is enabled in ERPNext.
6. **Write separate Buying and Selling rates** and **Also write the inverse pair** as needed.
7. Tick **Enabled**, save, and press **Sync now** to check the result.

### Rate direction

ERPNext reads a Currency Exchange record as *1 From Currency = Exchange Rate To Currency*. RBI publishes 1 USD = 95.25 INR, so the app writes From = USD, To = INR, Exchange Rate = 95.25. Currencies quoted per 100 units (JPY, IDR, KRW) arrive already converted to per-unit rates.

### Buying and selling

ERPNext uses the **For Buying** record on purchase documents and **For Selling** on sales documents. With the split on:

- Bank TT card: the bank's TT **selling** rate goes to For Buying (you buy foreign currency to pay a supplier), the TT **buying** rate goes to For Selling (the bank buys the foreign currency your customer paid).
- CBIC: the **import** rate goes to For Buying, the **export** rate to For Selling.

When you switch the split on or off, the app replaces its own record for the same date and pair, so ERPNext never sees a combined record and a split record side by side.

### ERPNext provider preset

ERPNext only consults Currency Exchange Settings when no stored record covers a pair and date. **Apply ERPNext provider preset** sets it to:

- Without an API key: the keyless ECB endpoint `https://allratestoday.com/api/compat/frankfurter/{transaction_date}`, parameters `base={from_currency}`, `symbols={to_currency}`, result key `rates` then `{to_currency}`.
- With a paid-plan API key and the RBI, ECB or other central bank source: that bank's dated table, `https://allratestoday.com/api/v1/central-bank/rbi/{transaction_date}`, parameters `source`, `target`, `api_key`, result key `rate`. ERPNext stores this key in Currency Exchange Settings in plain text, like any provider key.

ERPNext tests the settings on save with a USD to INR request for today. The full field-by-field guide is at [allratestoday.com/migrate/erpnext/](https://allratestoday.com/migrate/erpnext/).

## Privacy

- Keyless sources (RBI, CBIC, ECB, bank TT cards, other central banks) are plain HTTPS GET requests for a public table. They send no API key, no company data, no amounts and no user information.
- Live market rates send your API key in an `Authorization` header and the list of currency codes you asked for. Nothing else.
- The app stores rates only in your own ERPNext database.

## Data and attribution

Official rates are published by the named central bank or tax authority and served by AllRatesToday. Bank TT card rates are the indicative board rates each bank shows on its own website at capture time; they are not official or reference rates. Rates stay inside your ERPNext site; if you show them on a public page, include a visible link to allratestoday.com.

More about Indian sources: [allratestoday.com/exchange-rate-api-india/](https://allratestoday.com/exchange-rate-api-india/).

## Development

```bash
pip install pytest
pytest
```

`allratestoday/api.py` is pure Python (standard library only) and is tested against responses recorded from the live API in `tests/fixtures/`. `allratestoday/sync.py` holds the ERPNext glue.

## Support

- Issues: [github.com/AllRates-Today/allratestoday-frappe/issues](https://github.com/AllRates-Today/allratestoday-frappe/issues)
- Email: support@allratestoday.com

## License

MIT. See [LICENSE](LICENSE).
