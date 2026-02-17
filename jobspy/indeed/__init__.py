from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Tuple

from jobspy.indeed.constant import job_search_query, api_headers
from jobspy.indeed.util import is_job_remote, get_compensation, get_job_type
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    Location,
    JobResponse,
    JobType,
    DescriptionFormat,
)
import requests as req_lib

from jobspy.util import (
    extract_emails_from_text,
    markdown_converter,
    create_session,
    create_logger,
)

log = create_logger("Indeed")


class Indeed(Scraper):
    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        """
        Initializes IndeedScraper with the Indeed API url
        """
        super().__init__(Site.INDEED, proxies=proxies)

        # Main session for GraphQL API — no proxy (datacenter IPs work fine)
        self.session = create_session(
            proxies=None, ca_cert=ca_cert, is_tls=False
        )
        # TLS session without proxy for fallback (browser-like TLS fingerprint)
        self.html_session = create_session(
            proxies=None, ca_cert=ca_cert, is_tls=True
        )
        # Proxy config for fallback retry if TLS session also gets blocked
        self._fallback_proxies = None
        self._ca_cert = ca_cert if ca_cert else False
        if self.proxies:
            if isinstance(self.proxies, str):
                self._fallback_proxies = {"http": self.proxies, "https": self.proxies}
            elif isinstance(self.proxies, list) and self.proxies:
                self._fallback_proxies = {"http": self.proxies[0], "https": self.proxies[0]}
        self.scraper_input = None
        self.jobs_per_page = 100
        self.num_workers = 10
        self.seen_urls = set()
        self.headers = None
        self.api_country_code = None
        self.base_url = None
        self.api_url = "https://apis.indeed.com/graphql"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Indeed for jobs with scraper_input criteria
        :param scraper_input:
        :return: job_response
        """
        self.scraper_input = scraper_input
        domain, self.api_country_code = self.scraper_input.country.indeed_domain_value
        self.base_url = f"https://{domain}.indeed.com"
        self.headers = api_headers.copy()
        self.headers["indeed-co"] = self.scraper_input.country.indeed_domain_value
        job_list = []
        page = 1

        cursor = None

        while len(self.seen_urls) < scraper_input.results_wanted + scraper_input.offset:
            log.info(
                f"search page: {page} / {math.ceil(scraper_input.results_wanted / self.jobs_per_page)}"
            )
            jobs, cursor = self._scrape_page(cursor)
            if not jobs:
                log.info(f"found no jobs on page: {page}")
                break
            job_list += jobs
            page += 1
        return JobResponse(
            jobs=job_list[
                scraper_input.offset : scraper_input.offset
                + scraper_input.results_wanted
            ]
        )

    def _scrape_page(self, cursor: str | None) -> Tuple[list[JobPost], str | None]:
        """
        Scrapes a page of Indeed for jobs with scraper_input criteria
        :param cursor:
        :return: jobs found on page, next page cursor
        """
        jobs = []
        new_cursor = None
        filters = self._build_filters()
        search_term = (
            self.scraper_input.search_term.replace('"', '\\"')
            if self.scraper_input.search_term
            else ""
        )
        query = job_search_query.format(
            what=(f'what: "{search_term}"' if search_term else ""),
            location=(
                f'location: {{where: "{self.scraper_input.location}", radius: {self.scraper_input.distance}, radiusUnit: MILES}}'
                if self.scraper_input.location
                else ""
            ),
            dateOnIndeed=self.scraper_input.hours_old,
            cursor=f'cursor: "{cursor}"' if cursor else "",
            filters=filters,
        )
        payload = {
            "query": query,
        }
        api_headers_temp = api_headers.copy()
        api_headers_temp["indeed-co"] = self.api_country_code
        response = self.session.post(
            self.api_url,
            headers=api_headers_temp,
            json=payload,
            timeout=10,
            verify=False,
        )
        if not response.ok:
            log.info(
                f"responded with status code: {response.status_code} (submit GitHub issue if this appears to be a bug)"
            )
            return jobs, new_cursor
        data = response.json()
        jobs = data["data"]["jobSearch"]["results"]
        new_cursor = data["data"]["jobSearch"]["pageInfo"]["nextCursor"]

        job_list = []
        for job in jobs:
            processed_job = self._process_job(job["job"])
            if processed_job:
                job_list.append(processed_job)

        return job_list, new_cursor

    def _build_filters(self):
        """
        Builds the filters dict for job type/is_remote. If hours_old is provided, composite filter for job_type/is_remote is not possible.
        IndeedApply: filters: { keyword: { field: "indeedApplyScope", keys: ["DESKTOP"] } }
        """
        filters_str = ""
        if self.scraper_input.hours_old:
            filters_str = """
            filters: {{
                date: {{
                  field: "dateOnIndeed",
                  start: "{start}h"
                }}
            }}
            """.format(
                start=self.scraper_input.hours_old
            )
        elif self.scraper_input.easy_apply:
            filters_str = """
            filters: {
                keyword: {
                  field: "indeedApplyScope",
                  keys: ["DESKTOP"]
                }
            }
            """
        elif self.scraper_input.job_type or self.scraper_input.is_remote:
            job_type_key_mapping = {
                JobType.FULL_TIME: "CF3CP",
                JobType.PART_TIME: "75GKK",
                JobType.CONTRACT: "NJXCK",
                JobType.INTERNSHIP: "VDTG7",
            }

            keys = []
            if self.scraper_input.job_type:
                key = job_type_key_mapping[self.scraper_input.job_type]
                keys.append(key)

            if self.scraper_input.is_remote:
                keys.append("DSQF7")

            if keys:
                keys_str = '", "'.join(keys)
                filters_str = f"""
                filters: {{
                  composite: {{
                    filters: [{{
                      keyword: {{
                        field: "attributes",
                        keys: ["{keys_str}"]
                      }}
                    }}]
                  }}
                }}
                """
        return filters_str

    def _fetch_company_name(self, job_url: str) -> str | None:
        """
        Falls back to fetching the job page and extracting the company name
        from the JSON-LD structured data when the API does not return one.
        Tries without proxy first, falls back to proxy if blocked (403/401).
        """
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            # Try with TLS session first (browser-like fingerprint, no proxy)
            response = self.html_session.get(job_url, headers=headers)
            # If blocked, retry with proxy via plain requests
            if not response.ok and self._fallback_proxies:
                log.info(f"_fetch_company_name: TLS got {response.status_code}, retrying with proxy for {job_url}")
                response = req_lib.get(
                    job_url, headers=headers, proxies=self._fallback_proxies,
                    timeout=10, verify=self._ca_cert
                )
            if not response.ok:
                log.warning(f"_fetch_company_name: HTTP {response.status_code} for {job_url}")
                return None
            matches = re.findall(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                response.text,
                re.DOTALL,
            )
            for match in matches:
                try:
                    data = json.loads(match)
                    name = data.get("hiringOrganization", {}).get("name")
                    if name:
                        return name
                except (json.JSONDecodeError, AttributeError):
                    continue
            log.warning(f"_fetch_company_name: no hiringOrganization found in JSON-LD for {job_url}")
        except Exception as e:
            log.warning(f"_fetch_company_name: exception for {job_url}: {e}")
        return None

    def _process_job(self, job: dict) -> JobPost | None:
        """
        Parses the job dict into JobPost model
        :param job: dict to parse
        :return: JobPost if it's a new job
        """
        job_url = f'{self.base_url}/viewjob?jk={job["key"]}'
        if job_url in self.seen_urls:
            return
        self.seen_urls.add(job_url)
        description = job["description"]["html"]
        if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
            description = markdown_converter(description)

        job_type = get_job_type(job["attributes"])
        timestamp_seconds = job["datePublished"] / 1000
        date_posted = datetime.fromtimestamp(timestamp_seconds).strftime("%Y-%m-%d")
        employer = job["employer"].get("dossier") if job["employer"] else None
        employer_details = employer.get("employerDetails", {}) if employer else {}
        rel_url = job["employer"]["relativeCompanyPageUrl"] if job["employer"] else None
        company_name = job["employer"].get("name") if job.get("employer") else None
        if not company_name:
            company_name = self._fetch_company_name(job_url)
        return JobPost(
            id=f'in-{job["key"]}',
            title=job["title"],
            description=description,
            company_name=company_name,
            company_url=(f"{self.base_url}{rel_url}" if job["employer"] else None),
            company_url_direct=(
                employer["links"]["corporateWebsite"] if employer else None
            ),
            location=Location(
                city=job.get("location", {}).get("city"),
                state=job.get("location", {}).get("admin1Code"),
                country=job.get("location", {}).get("countryCode"),
            ),
            job_type=job_type,
            compensation=get_compensation(job["compensation"]),
            date_posted=date_posted,
            job_url=job_url,
            job_url_direct=(
                job["recruit"].get("viewJobUrl") if job.get("recruit") else None
            ),
            emails=extract_emails_from_text(description) if description else None,
            is_remote=is_job_remote(job, description),
            company_addresses=(
                employer_details["addresses"][0]
                if employer_details.get("addresses")
                else None
            ),
            company_industry=(
                employer_details["industry"]
                .replace("Iv1", "")
                .replace("_", " ")
                .title()
                .strip()
                if employer_details.get("industry")
                else None
            ),
            company_num_employees=employer_details.get("employeesLocalizedLabel"),
            company_revenue=employer_details.get("revenueLocalizedLabel"),
            company_description=employer_details.get("briefDescription"),
            company_logo=(
                employer["images"].get("squareLogoUrl")
                if employer and employer.get("images")
                else None
            ),
        )
