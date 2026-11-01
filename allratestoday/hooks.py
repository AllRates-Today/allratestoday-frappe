app_name = "allratestoday"
app_title = "AllRatesToday Exchange Rates"
app_publisher = "AllRatesToday"
app_description = "Load official exchange rates (RBI/FBIL, CBIC customs, Indian bank TT cards, ECB, 120+ central banks) or live market rates into ERPNext Currency Exchange"
app_email = "support@allratestoday.com"
app_license = "MIT"
app_icon = "octicon octicon-graph"
app_color = "#2563eb"

required_apps = ["erpnext"]

scheduler_events = {
	"daily": ["allratestoday.sync.scheduled_daily"],
	"hourly": ["allratestoday.sync.scheduled_hourly"],
}
