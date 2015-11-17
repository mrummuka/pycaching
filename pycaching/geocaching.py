#!/usr/bin/env python3

import logging
import datetime
import requests
import bs4
import json
from urllib.parse import urljoin
from os import path
from pycaching.cache import Cache, Type, Size
from pycaching.log import Log, Type as LogType
from pycaching.geo import Point
from pycaching.trackable import Trackable
from pycaching.errors import Error, NotLoggedInException, LoginFailedException
from pycaching.util import parse_date, deprecated


class Geocaching(object):
    """Provides some basic methods for communicating with geocaching.com website.

    Provides methods to login and search. There are also some shortcut methods in this class to make
    working with pycaching more convinient.
    """

    _baseurl = "https://www.geocaching.com"
    _urls = {
        "login_page":        "login/default.aspx",
        "search":            "play/search",
        "search_more":       "play/search/more-results",
    }
    _credentials_file = ".gc_credentials"

    def __init__(self):
        self._logged_in = False
        self._session = requests.Session()

    def _request(self, url, *, expect="soup", method="GET", login_check=True, **kwargs):
        """
        Do a HTTP request and return a response based on expect param.

        :param url: Request target.
        :param method: HTTP method to use.
        :param expect: Expected type of data (either `soup`, `json` or `raw`).
        :param login_check: Whether to check if user is logged in or not.
        :param kwargs: Passed to :func:`requests.request` as is.
        """
        # check login unless explicitly turned off
        if login_check and self._logged_in is False:
            raise NotLoggedInException("Login is needed.")

        url = url if "//" in url else urljoin(self._baseurl, url)

        try:
            res = self._session.request(method, url, **kwargs)
            res.raise_for_status()

            # return bs4.BeautifulSoup, JSON dict or raw requests.Response
            if expect == "soup":
                return bs4.BeautifulSoup(res.text, "html.parser")
            elif expect == "json":
                return res.json()
            elif expect == "raw":
                return res

        except requests.exceptions.RequestException as e:
            raise Error("Cannot load page: {}".format(url)) from e

    def login(self, username=None, password=None):
        """Log in the user for this instance of Geocaching.

        If username or password is not set, try to load credentials from file. Then load login page
        and do some checks about currently logged user. As a last thing post the login form and
        check result. Raise :class:`LoginFailedException` if something fails.
        """
        if not username or not password:
            try:
                username, password = self._load_credentials()
            except FileNotFoundError as e:
                raise LoginFailedException("Credentials file not found and "
                                           "no username and password is given.") from e
            except ValueError as e:
                raise LoginFailedException("Wrong format of credentials file.") from e
            except KeyError as e:
                raise LoginFailedException("Credentials file doesn't "
                                           "contain username and password.") from e
            except IOError as e:
                raise LoginFailedException("Credentials file reading error.") from e

        logging.info("Logging in...")
        login_page = self._request(self._urls["login_page"], login_check=False)

        logging.debug("Checking for previous login.")
        logged = self.get_logged_user(login_page)
        if logged:
            if logged == username:
                logging.info("Already logged as %s.", logged)
                self._logged_in = True
                return
            else:
                logging.info("Already logged as %s, but want to log in as %s.", logged, username)
                self.logout()

        # continue logging in
        post = {}
        logging.debug("Assembling POST data.")

        # login fields
        login_elements = login_page.find_all("input", type=["text", "password", "checkbox"])
        post.update({field["name"]: val for field, val in zip(
            login_elements, [username, password, 1])})

        # other nescessary fields
        other_elements = login_page.find_all("input", type=["hidden", "submit"])
        post.update({field["name"]: field["value"] for field in other_elements})

        # login to the site
        logging.debug("Submiting login form.")
        after_login_page = self._request(
            self._urls["login_page"], method="POST", data=post, login_check=False)

        logging.debug("Checking the result.")
        if self.get_logged_user(after_login_page):
            logging.info("Logged in successfully as %s.", username)
            self._logged_in = True
            return
        else:
            self.logout()
            raise LoginFailedException("Cannot login to the site "
                                       "(probably wrong username or password).")

    def _load_credentials(self):
        """Load credentials from file.

        Find credentials file in either current directory or user's home directory. If exists, load
        it as a JSON and returns credentials from it.

        :returns: Tuple of username and password loaded from file.
        :raises: FileNotFoundError
        """
        credentials_file = self._credentials_file

        # find the location of a file
        if path.isfile(credentials_file):
            logging.info("Loading credentials file from current directory")
        else:
            credentials_file = path.join(path.expanduser("~"), self._credentials_file)
            if path.isfile(credentials_file):
                logging.info("Loading credentials file form home directory")
            else:
                raise FileNotFoundError("Credentials file not found in current nor home directory.")

        # load contents
        with open(credentials_file, "r") as f:
            credentials = json.load(f)
            return credentials["username"], credentials["password"]

    def logout(self):
        """Log out the user for this instance."""
        logging.info("Logging out.")
        self._logged_in = False
        self._session = requests.Session()

    def get_logged_user(self, login_page=None):
        """Return the name of currently logged user.

        :param login_page: :class:`bs4.BeautifulSoup` object containing already loaded page.
        :returns: User's name or `None`, if no user is logged in.
        """
        login_page = login_page or self._request(self._urls["login_page"], login_check=False)
        assert hasattr(login_page, "find") and callable(login_page.find)

        logging.debug("Checking for already logged user.")
        try:
            return login_page.find("div", "LoggedIn").find("strong").text
        except AttributeError:
            return None

    def search(self, point, limit=float("inf")):
        """Return a generator of caches around some point.

        Search for caches around some point by loading search pages and parsing the data from these
        pages. Yield :class:`Cache` objects filled with data from search page. You can provide limit
        as a convinient way to stop generator after certain number of caches.

        :param point: Search center point.
        :param limit: Maximum number of caches to generate.
        """
        logging.info("Searching at {}".format(point))

        start_index = 0
        while True:
            # get one page
            page = self._search_get_page(point, start_index)

            if not page:
                # result is empty - no more caches
                raise StopIteration()

            # parse caches in result
            for start_index, row in enumerate(page.find_all("tr"), start_index):

                limit -= 1  # handle limit
                if limit < 0:
                    raise StopIteration()

                # parse raw data
                cache_details = row.find("span", "cache-details").text.split("|")
                wp = cache_details[1].strip()

                # create and fill cache object
                c = Cache(self, wp)
                c.type = Type.from_string(cache_details[0].strip())
                c.name = row.find("span", "cache-name").text
                c.found = row.find("img", title="Found It!") is not None
                c.favorites = int(row.find(attrs={"data-column": "FavoritePoint"}).text)
                c.state = not (row.get("class") and "disabled" in row.get("class"))
                c.pm_only = row.find("td", "pm-upsell") is not None

                if c.pm_only:
                    # PM only caches doesn't have other attributes filled in
                    yield c
                    continue

                c.size = Size.from_string(row.find(attrs={"data-column": "ContainerSize"}).text)
                c.difficulty = float(row.find(attrs={"data-column": "Difficulty"}).text)
                c.terrain = float(row.find(attrs={"data-column": "Terrain"}).text)
                c.hidden = parse_date(row.find(attrs={"data-column": "PlaceDate"}).text)
                c.author = row.find("span", "owner").text[3:]  # delete "by "

                logging.debug("Cache parsed: %s", c)
                yield c

            start_index += 1

    def _search_get_page(self, point, start_index):
        """Return one page for standard search as class:`bs4.BeautifulSoup` object.

        :param point: Search center point.
        :param start_index: Determines the page. If start_index is greater than zero, this method
            will use AJAX andpoint which is much faster.
        """
        assert hasattr(point, "format") and callable(point.format)
        logging.debug("Loading page from start_index {}".format(start_index))

        if start_index == 0:
            # first request has to load normal search page
            logging.debug("Using normal search endpoint")

            # make request
            res = self._request(self._urls["search"], params={
                "origin": point.format(None, "", "", "")
            })
            return res.find(id="geocaches")

        else:
            # other requests can use AJAX endpoint
            logging.debug("Using AJAX search endpoint")

            # make request
            res = self._request(self._urls["search_more"], params={
                "inputOrigin": point.format(None, "", "", ""),
                "startIndex": start_index,
                "originTreatment": 0
            }, expect="json")

            return bs4.BeautifulSoup(res["HtmlString"].strip(), "html.parser")

    def search_quick(self, area, *, strict=False, zoom=None):
        """Return a generator of caches in some area.

        Area is converted to map tiles, each tile is then loaded and :class:`Cache` objects are then
        created from its blocks.

        :param strict: Whether to return caches strictly in the `area` and discard the outside ones.
        :param zoom: Zoom level of tiles. You can also specify it manually, otherwise it is
            automatically determined for whole :class:`Area` to fit into one :class:`Tile`. Higher
            zoom level is more precise, but requires more tiles to be loaded.
        """
        logging.info("Searching quick in {}".format(area))

        tiles = area.to_tiles(self, zoom)
        # TODO process tiles by multiple workers
        for tile in tiles:
            for block in tile.blocks:
                cache = Cache.from_block(block)
                if strict and cache.location not in area:
                    # if strict mode is on and cache is not in area
                    continue
                else:
                    # can yield more caches (which are not exactly in desired area)
                    yield cache

    # add some shortcuts ------------------------------------------------------

    def geocode(self, location):
        """Return a :class:`Point` object from geocoded location."""
        return Point.from_location(self, location)

    def get_cache(self, wp):
        """Return a :class:`Cache` object by its waypoint."""
        return Cache(self, wp)

    def get_trackable(self, tid):
        """Return a :class:`Trackable` object by its trackable ID."""
        return Trackable(self, tid)

    def post_log(self, wp, text, type=LogType.found_it, date=datetime.date.today()):
        """Post a log for cache.

        :param wp: Cache waypoint.
        :param text: :class:`Log` text.
        :param type: Type of log (enum :class:`log.Type`).
        :param date: Date of cache visit.
        """
        l = Log(type=type, text=text, visited=date)
        self.get_cache(wp).post_log(l)

    # ensure backwards compatibility ------------------------------------------
    # deprecated methods will be removed in next version!

    @deprecated
    def load_cache(self, wp):
        """Return a :class:`Cache` object by its waypoint."""
        return self.get_cache(wp)

    @deprecated
    def load_trackable(self, tid):
        """Return a :class:`Trackable` object by its trackable ID."""
        return self.get_trackable(tid)
