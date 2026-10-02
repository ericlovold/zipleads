import json
from pathlib import Path

import pytest

from zipleads.sorter import (
    BUILDING_SYSTEMS,
    KINDS,
    NEW_OCCUPANT,
    RESIDENTIAL,
    TENANT_REFRESH,
    sort_permit,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "permit_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_real_permits_sort_as_labeled(case):
    """Real Minneapolis permits, labeled from the rep's feedback. Development set."""
    v = sort_permit(
        permit_type=case["permit_type"],
        work_type=case["work_type"],
        occupancy=case["occupancy"],
        description=case["description"],
        applicant=case["applicant"],
    )
    assert v.kind == case["expect"], f"{case['why']} -> got {v.kind}: {v.reasons}"
    assert v.reasons, "every verdict must explain itself"


# Wording written to probe generalization before the rules saw it. Three of these
# failed on first run (future tenant, spec suite, relocating) and drove the fix.
WRITTEN = [
    (NEW_OCCUPANT, "Commercial", "Remodel", "Interior demolition for future tenant, suite 140", ""),
    (NEW_OCCUPANT, "Commercial", "Remodel", "Change of use from retail to dental office", ""),
    (NEW_OCCUPANT, "Commercial", "Remodel", "Restaurant buildout - Pho 79", ""),
    (
        NEW_OCCUPANT,
        "Mechanical",
        "ComAlt",
        "Install new rooftop unit and ductwork for new tenant",
        "",
    ),
    (NEW_OCCUPANT, "Commercial", "Remodel", "Spec suite build for lease, vanilla box", ""),
    (
        NEW_OCCUPANT,
        "Commercial",
        "Remodel",
        "Law firm relocating to 4th floor, interior finish",
        "",
    ),
    (
        TENANT_REFRESH,
        "Commercial",
        "Remodel",
        "Remodel existing restaurant kitchen and dining room",
        "",
    ),
    (
        TENANT_REFRESH,
        "Commercial",
        "Remodel",
        "Tenant improvement for existing tenant expansion into adjacent suite",
        "",
    ),
    (TENANT_REFRESH, "Commercial", "Remodel", "Interior remodel of bank branch, new finishes", ""),
    (BUILDING_SYSTEMS, "Commercial", "Remodel", "Install new windows on east elevation", ""),
    (BUILDING_SYSTEMS, "Electrical", "Comm", "Panel upgrade 400A", ""),
    (BUILDING_SYSTEMS, "Commercial", "Remodel", "Repair water damage, 2nd floor corridor", ""),
    (RESIDENTIAL, "Commercial", "Remodel", "Repair water damage in apartment unit 3", ""),
    (RESIDENTIAL, "Commercial", "Remodel", "Kitchen remodel unit 5B", "Lakeview Condominium Assn"),
    (
        BUILDING_SYSTEMS,
        "Wrecking",
        "Private",
        "Wreck existing office building to make way for new office development",
        "",
    ),
    (NEW_OCCUPANT, "Commercial", "Remodel", "Restaurant under new ownership, interior refresh", ""),
]


@pytest.mark.parametrize(
    "expect,ptype,wtype,desc,applicant", WRITTEN, ids=[w[3][:40] for w in WRITTEN]
)
def test_written_wording(expect, ptype, wtype, desc, applicant):
    v = sort_permit(
        permit_type=ptype, work_type=wtype, occupancy="Comm", description=desc, applicant=applicant
    )
    assert v.kind == expect, v.reasons


def test_residential_wins_over_new_occupant_clues():
    # "new location" appears, but the Res code means it is a house.
    v = sort_permit(
        permit_type="Plumbing",
        work_type="Res",
        description="Island kitchen sink repipe in new location",
    )
    assert v.kind == RESIDENTIAL and v.confidence == "high"


def test_trade_permit_for_a_new_business_is_a_lead():
    v = sort_permit(
        permit_type="Plumbing",
        work_type="FoodBev",
        description="plumbing remodel for new smoothie store",
    )
    assert v.kind == NEW_OCCUPANT


def test_low_confidence_calls_are_marked():
    assert (
        sort_permit(
            permit_type="Commercial", work_type="Remodel", description="REMODEL UNIT 104"
        ).confidence
        == "low"
    )
    assert (
        sort_permit(
            permit_type="Commercial", work_type="Remodel", description="Remodel second floor"
        ).kind
        == TENANT_REFRESH
    )


def test_every_kind_is_reachable_and_known():
    assert set(KINDS) == {NEW_OCCUPANT, TENANT_REFRESH, BUILDING_SYSTEMS, RESIDENTIAL}
