import pytest
from unittest import mock
from openstates.scrape import Bill, State, EmptyScrape
from openstates.scrape.base import Scraper, ScrapeError, BaseBillScraper


class NewJersey(State):
    pass


juris = NewJersey()


def test_save_object_basics():
    # ensure that save object dumps a file
    s = Scraper(juris, "/tmp/")
    p = Bill("HB 1", "2021", "Test")
    p.add_source("http://example.com")

    with mock.patch("json.dump") as json_dump:
        s.save_object(p)

    # ensure object is saved in right place
    filename = "bill_" + p._id + ".json"
    assert filename in s.output_names["bill"]
    json_dump.assert_called_once_with(p.as_dict(), mock.ANY, cls=mock.ANY)


def test_save_object_invalid():
    s = Scraper(juris, "/tmp/")
    p = Bill("HB 1", "2021", "Test")
    # no source, won't validate

    with pytest.raises(ValueError):
        s.save_object(p)


def test_save_related():
    s = Scraper(juris, "/tmp/")
    p = Bill("HB 1", "2021", "Test")
    p.add_source("http://example.com")
    o = Bill("HB 2", "2021", "Test")
    o.add_source("http://example.com")
    p._related.append(o)

    with mock.patch("json.dump") as json_dump:
        s.save_object(p)

    assert json_dump.mock_calls == [
        mock.call(p.as_dict(), mock.ANY, cls=mock.ANY),
        mock.call(o.as_dict(), mock.ANY, cls=mock.ANY),
    ]


def test_simple_scrape():
    class FakeScraper(Scraper):
        def scrape(self):
            p = Bill("HB 1", "2021", "Test")
            p.add_source("http://example.com")
            yield p

    with mock.patch("json.dump") as json_dump:
        record = FakeScraper(juris, "/tmp/").do_scrape()

    assert len(json_dump.mock_calls) == 1
    assert record["objects"]["bill"] == 1
    assert record["end"] > record["start"]
    assert record["skipped"] == 0


def test_double_iter():
    """ tests that scrapers that yield iterables work OK """

    class IterScraper(Scraper):
        def scrape(self):
            yield self.scrape_people()

        def scrape_people(self):
            p = Bill("HB 1", "2021", "The Club")
            p.add_source("http://example.com")
            yield p

    with mock.patch("json.dump") as json_dump:
        record = IterScraper(juris, "/tmp/").do_scrape()

    assert len(json_dump.mock_calls) == 1
    assert record["objects"]["bill"] == 1


def test_no_objects():
    class NullScraper(Scraper):
        def scrape(self):
            pass

    with pytest.raises(ScrapeError):
        NullScraper(juris, "/tmp/", fastmode=True).do_scrape()


def test_no_objects_empty_scrape():
    class NullScraper(Scraper):
        def scrape(self):
            raise EmptyScrape()

    # doesn't raise despite yielding zero objects
    NullScraper(juris, "/tmp/", fastmode=True).do_scrape()


def test_empty_scrape_with_objects():
    class TestScraper(Scraper):
        def scrape(self):
            p = Bill("HB 6", "2021", "Don Jaggerty")
            p.add_source("https://example.com")
            yield p
            raise EmptyScrape()

    # can't yield objects and raise EmptyScrape
    with pytest.raises(ScrapeError):
        TestScraper(juris, "/tmp/", fastmode=True).do_scrape()


def test_no_scrape():
    class NonScraper(Scraper):
        pass

    with pytest.raises(NotImplementedError):
        NonScraper(juris, "/tmp/").do_scrape()


def test_bill_scraper():
    class BillScraper(BaseBillScraper):
        def get_bill_ids(self):
            yield "1", {"extra": "param"}
            yield "2", {}

        def get_bill(self, bill_id, **kwargs):
            if bill_id == "1":
                assert kwargs == {"extra": "param"}
                raise self.ContinueScraping
            else:
                assert bill_id == "2"
                assert kwargs == {}
                b = Bill("1", self.legislative_session, "title")
                b.add_source("http://example.com")
                return b

    bs = BillScraper(juris, "/tmp/")
    with mock.patch("json.dump") as json_dump:
        record = bs.do_scrape(legislative_session="2020")

    assert len(json_dump.mock_calls) == 1
    assert record["objects"]["bill"] == 1
    assert record["skipped"] == 1


def test_whitespace_is_stripped():
    s = Scraper(juris, "/tmp/")
    b = Bill(" HB 11", "2020", " a short title     ")
    b.subject = [" one", "two ", "   three "]
    b.add_source("https://example.com/     ")

    s.save_object(b)

    # the simple cases, and nested lists / objects
    assert b.identifier == "HB 11"
    assert b.title == "a short title"
    assert b.sources[0]["url"] == "https://example.com/"
    # subject got sorted by pre_save
    assert b.subject == ["one", "three", "two"]


def test_normalize_action_dates():
    """Test date normalization handles various edge cases safely."""
    # Create scraper in fastmode to enable Elasticsearch comparison
    s = Scraper(juris, "/tmp/", fastmode=True)
    
    # Create a bill with actions
    b1 = Bill("HB 1", "2023", "Test Bill")
    b1.add_action("introduced", "2023-01-15T10:30:00Z")
    b1.add_action("passed", "2023-01-15")
    b1.add_source("http://example.com")
    
    b2 = Bill("HB 1", "2023", "Test Bill")
    b2.add_action("introduced", "2023-01-15")  # Same date, different format
    b2.add_action("passed", "2023-01-15")
    b2.add_source("http://example.com")
    
    # These should be considered identical after normalization
    # We'll test this by mocking the existing_session_bills
    s.existing_session_bills = {"HB 1": b1.as_dict()}
    
    with mock.patch("json.dump") as json_dump:
        s.save_object(b2)
    
    # The bill should NOT be saved because actions are identical after normalization
    assert len(json_dump.mock_calls) == 0


def test_bill_comparison_with_different_action_formats():
    """Test that bills with different action date formats are properly compared."""
    # Create scraper in fastmode to enable Elasticsearch comparison
    s = Scraper(juris, "/tmp/", fastmode=True)
    
    # Create two identical bills with different date formats
    b1 = Bill("HB 1", "2023", "Test Bill")
    b1.add_action("introduced", "2023-01-15T10:30:00Z")
    b1.add_source("http://example.com")
    
    b2 = Bill("HB 1", "2023", "Test Bill") 
    b2.add_action("introduced", "2023-01-15")  # Same date, different format
    b2.add_source("http://example.com")
    
    # Mock existing bills
    s.existing_session_bills = {"HB 1": b1.as_dict()}
    
    with mock.patch("json.dump") as json_dump:
        s.save_object(b2)
    
    # Should be considered identical and not saved again
    assert len(json_dump.mock_calls) == 0


def test_bill_comparison_with_different_actions():
    """Test that bills with genuinely different actions are not considered identical."""
    s = Scraper(juris, "/tmp/")
    
    # Create two bills with different actions
    b1 = Bill("HB 1", "2023", "Test Bill")
    b1.add_action("introduced", "2023-01-15")
    b1.add_source("http://example.com")
    
    b2 = Bill("HB 1", "2023", "Test Bill")
    b2.add_action("introduced", "2023-01-15")
    b2.add_action("passed", "2023-01-16")  # Additional action
    b2.add_source("http://example.com")
    
    # Mock existing bills
    s.existing_session_bills = {"HB 1": b1.as_dict()}
    
    with mock.patch("json.dump") as json_dump:
        s.save_object(b2)
    
    # Should be considered different and saved
    assert len(json_dump.mock_calls) == 1


def test_normalize_action_dates_edge_cases():
    """Test edge cases for date normalization that could cause crashes."""
    s = Scraper(juris, "/tmp/")
    
    # Test with valid date formats that should be normalized
    # (We can't test with invalid dates because the Bill schema validates them)
    actions = [
        {"date": "2023-01-15T10:30:00Z", "action": "normal"},  # Should work
        {"date": "2023-01-15", "action": "already_short"},     # Should work
        {"date": "  2023-01-15T10:30:00Z  ", "action": "whitespace"},  # Should work
    ]
    
    # Create a bill with these actions
    b = Bill("HB 1", "2023", "Test Bill")
    for action in actions:
        if action["date"]:
            b.add_action(action["action"], action["date"])
    b.add_source("http://example.com")
    
    # This should not crash
    with mock.patch("json.dump") as json_dump:
        s.save_object(b)
    
    # Should save successfully
    assert len(json_dump.mock_calls) == 1
