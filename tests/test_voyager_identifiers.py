"""Every tool's output names an id the way the next tool's input takes it."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.voyager.client import company_id, person_identifier

URN = "urn:li:fsd_profile:ACoAAADE1mABtyeGpFNUfvunkA1wMxiTsyYisBU"


@pytest.mark.parametrize(
    ("url", "urn", "expected"),
    [
        # A vanity name wins when LinkedIn gave one.
        ("https://www.linkedin.com/in/ilan-rado-2a83a14", URN, "ilan-rado-2a83a14"),
        # Messaging usually gives only the obfuscated id: the URN's id is
        # used, which the profile finder resolves to the same member.
        (
            "https://www.linkedin.com/in/ACoAAADE1mABtyeGpFNUfvunkA1wMxiTsyYisBU",
            URN,
            "ACoAAADE1mABtyeGpFNUfvunkA1wMxiTsyYisBU",
        ),
        ("", URN, "ACoAAADE1mABtyeGpFNUfvunkA1wMxiTsyYisBU"),
        (None, None, None),
    ],
)
def test_a_person_identifier_is_what_linkedin_username_takes(url, urn, expected):
    assert person_identifier(url, urn) == expected


@pytest.mark.parametrize(
    ("urn", "expected"),
    [
        ("urn:li:fsd_company:1441", "1441"),
        ("urn:li:fs_normalized_company:17988315", "17988315"),
        ("urn:li:fsd_company:not-a-number", None),
        (None, None),
    ],
)
def test_a_company_id_is_what_company_filters_take(urn, expected):
    assert company_id(urn) == expected
