import frappe
from connectfest.services.stalls import seed_default_stalls


def execute():
	"""
	Migration patch executed during `bench migrate` (post model sync).
	Seeds default stalls, dynamic tier pricing, and default settings.
	"""
	seed_default_stalls(preserve_booked=True)
