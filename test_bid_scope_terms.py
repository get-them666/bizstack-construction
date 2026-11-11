"""Job-specific terms must not leak onto every bid.

The assumptions and exclusions are letterhead-style boilerplate imported by
the scope of work, contract and package. One sewage-remediation constraint was
written into the global list, so every bid in the system -- including a
veterinary clinic fit-out -- claimed to have cleaned and disinfected
sewage-affected areas.
"""
import inspect

import bid_package_pdf as pkg
import build_bid_documents as docs

CLINIC = [{"description": "Drywall, paint and flooring", "category": "labor"}]
SEWAGE = [{"description": "Remove and haul away sewage-contaminated material",
           "category": "labor"}]


def test_a_generic_job_gets_no_sewage_language():
    assumptions = " ".join(pkg.assumptions_for(CLINIC)).lower()
    exclusions = " ".join(pkg.exclusions_for(CLINIC)).lower()
    assert "sewage" not in assumptions
    assert "sewage" not in exclusions


def test_a_sewage_job_still_gets_the_warning():
    assert "sewage" in " ".join(pkg.assumptions_for(SEWAGE)).lower()
    assert "sewage" in " ".join(pkg.exclusions_for(SEWAGE)).lower()


def test_documents_use_the_per_job_terms():
    scope = inspect.getsource(docs.build_scope_of_work)
    assert "assumptions_for(costs)" in scope
    assert "exclusions_for(costs)" in scope
    package = inspect.getsource(pkg.generate_bid_package_pdf)
    assert "assumptions_for(costs)" in package
    assert "exclusions_for(costs)" in package


def test_unpriced_bid_does_not_quote_zero_dollars():
    src = inspect.getsource(docs.build_scope_of_work)
    assert "Pricing has not been entered" in src
