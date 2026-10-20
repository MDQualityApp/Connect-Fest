import frappe
from connectfest.services.stalls import seed_default_stalls


def after_migrate():
	"""
	Hook executed automatically at the end of `bench migrate`.
	Ensures 195 standard stalls, pricing tiers, and settings exist in the database.
	"""
	try:
		seed_default_stalls(preserve_booked=True)
	except Exception as e:
		frappe.log_error(f"ConnectFest after_migrate error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_migrate warning: {e}")


def after_install():
	"""
	Hook executed automatically after the app is installed onto a site.
	"""
	try:
		seed_default_stalls(preserve_booked=True)
	except Exception as e:
		frappe.log_error(f"ConnectFest after_install error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_install warning: {e}")
