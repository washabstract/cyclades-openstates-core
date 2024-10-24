from .base import BaseModel, Scraper
from .popolo import Organization
from .schemas.jurisdiction import schema
from ..metadata import lookup
import requests


_name_fixes = {
    "SouthCarolina": "South Carolina",
    "NorthCarolina": "North Carolina",
    "SouthDakota": "South Dakota",
    "NorthDakota": "North Dakota",
    "RhodeIsland": "Rhode Island",
    "NewHampshire": "New Hampshire",
    "NewJersey": "New Jersey",
    "NewYork": "New York",
    "NewMexico": "New Mexico",
    "WestVirginia": "West Virginia",
    "PuertoRico": "Puerto Rico",
    "DistrictOfColumbia": "District of Columbia",
    "UnitedStates": "United States",
    "southafrica": "South Africa",
    "VirginIslands": "Virgin Islands",
    "AmericanSamoa": "American Samoa",
    "NorthernMarianaIslands": "Northern Mariana Islands",
}


class State(BaseModel):
    """ Base class for a jurisdiction """

    _type = "jurisdiction"
    _schema = schema

    # schema objects
    @property
    def legislative_sessions(self):
        return self.get_session_list()

    extras = {}

    # non-db properties
    scrapers = {}
    default_scrapers = None
    ignored_scraped_sessions = []
    _metadata = None

    def __init__(self):
        super(BaseModel, self).__init__()
        self._related = []
        self.extras = {}

    @property
    def classification(self):
        if any(c == self.name for c in ["United States", "South Africa"]):
            return "country"
        else:
            return "state"

    @property
    def metadata(self):
        if not self._metadata:
            name = _name_fixes.get(self.__class__.__name__, self.__class__.__name__)
            self._metadata = lookup(name=name)
        return self._metadata

    @property
    def division_id(self):
        return self.metadata.division_id

    @property
    def jurisdiction_id(self):
        return "{}/government".format(
            self.division_id.replace("ocd-division", "ocd-jurisdiction"),
        )

    @property
    def name(self):
        return self.metadata.name

    @property
    def url(self):
        return self.metadata.url

    def get_organizations(self):
        legislature = Organization(
            name=self.metadata.legislature_name, classification="legislature"
        )
        yield legislature
        if not self.metadata.unicameral:
            yield Organization(
                self.metadata.upper.name,
                classification="upper",
                parent_id=legislature._id,
            )
            yield Organization(
                self.metadata.lower.name,
                classification="lower",
                parent_id=legislature._id,
            )

    def get_session_list(self) -> list[str]:
        response = requests.get(self.sessions_url+"/sessions/query", params={"state_name": "California"})
        if response.status_code == 200:
            sessions = response.json()
            return [session["session_name"] for session in sessions]
        else:
            return []

    _id = jurisdiction_id

    def as_dict(self):
        return {
            "_id": self.jurisdiction_id,
            "id": self.jurisdiction_id,
            "name": self.name,
            "url": self.url,
            "division_id": self.division_id,
            "classification": self.classification,
            "legislative_sessions": self.legislative_sessions,
            "extras": self.extras,
        }

    def __str__(self):
        return self.name


class JurisdictionScraper(Scraper):
    def scrape(self):
        # yield a single Jurisdiction object
        yield self.jurisdiction

        # yield all organizations
        for org in self.jurisdiction.get_organizations():
            yield org
