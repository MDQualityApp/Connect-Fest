from connectfest.services.stalls import setup_default_stalls
from connectfest.services.sponsors import setup_default_sponsors
from connectfest.services.terms import setup_default_terms


def execute():
	"""
	Migration patch executed during `bench migrate` (post model sync).
	Initializes default stalls, dynamic tier pricing, default settings, sponsors, and terms.
	"""
	setup_default_stalls(preserve_booked=True)
	setup_default_sponsors()
	setup_default_terms()
