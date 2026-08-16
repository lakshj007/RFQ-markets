from kalshi_mm.matching import best_outcome_match, match_events, name_similarity
from kalshi_mm.odds import parse_event
from tests.test_odds import odds_event_payload


def kalshi_event() -> dict:
    return {
        "event_ticker": "KXMLBGAME-TEST",
        "title": "New York M vs Toronto",
        "markets": [
            {
                "ticker": "KXMLBGAME-TEST-NYM",
                "yes_sub_title": "New York M",
                "no_sub_title": "Toronto",
                "occurrence_datetime": "2026-07-04T20:05:00Z",
            },
            {
                "ticker": "KXMLBGAME-TEST-TOR",
                "yes_sub_title": "Toronto",
                "no_sub_title": "New York M",
                "occurrence_datetime": "2026-07-04T20:05:00Z",
            },
        ],
    }


def test_name_similarity_handles_kalshi_abbreviations() -> None:
    assert name_similarity("New York M", "New York Mets") > 0.8
    assert name_similarity("Toronto", "Toronto Blue Jays") >= 0.9


def test_draw_aliases_do_not_fuzzy_match_team_names() -> None:
    assert name_similarity("Tie", "Tigres") == 0
    assert best_outcome_match("Tie", ["Tigres", "Tijuana", "Draw"]) == ("Draw", 1.0)


def test_match_events_requires_participants_and_close_start_time() -> None:
    matches = match_events([kalshi_event()], [parse_event(odds_event_payload())])

    assert len(matches) == 1
    assert matches[0].odds_event.event_id == "event-1"
    assert matches[0].time_difference_seconds == 300
