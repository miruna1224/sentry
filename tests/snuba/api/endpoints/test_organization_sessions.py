import datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.urls import reverse
from freezegun import freeze_time

from sentry.release_health.metrics import MetricsReleaseHealthBackend
from sentry.testutils import APITestCase, SnubaTestCase
from sentry.testutils.cases import SessionMetricsTestCase
from sentry.testutils.helpers.link_header import parse_link_header
from sentry.utils.dates import to_timestamp


def result_sorted(result):
    """sort the groups of the results array by the `by` object, ensuring a stable order"""

    def stable_dict(d):
        return tuple(sorted(d.items(), key=lambda t: t[0]))

    result["groups"].sort(key=lambda group: stable_dict(group["by"]))
    return result


ONE_DAY_AGO = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=1)
TWO_DAYS_AGO = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=2)
MOCK_DATETIME = ONE_DAY_AGO.replace(hour=12, minute=27, second=28, microsecond=303000)
MOCK_DATETIME_PLUS_TEN_MINUTES = MOCK_DATETIME + datetime.timedelta(minutes=10)
SNUBA_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MOCK_DATETIME_START_OF_DAY = MOCK_DATETIME.replace(hour=0, minute=0, second=0)

TIMESTAMP = to_timestamp(MOCK_DATETIME)
RECEIVED = TIMESTAMP
SESSION_STARTED = TIMESTAMP // 3600 * 3600  # round to the hour

TEMPLATE = {
    "distinct_id": "00000000-0000-0000-0000-000000000000",
    "status": "exited",
    "seq": 0,
    "release": "foo@1.0.0",
    "environment": "production",
    "retention_days": 90,
    "duration": 123.4,
    "errors": 0,
    "started": SESSION_STARTED,
    "received": RECEIVED,
}


def make_duration(kwargs):
    """Randomish but deterministic duration"""
    return float(len(str(kwargs)))


def make_session(project, **kwargs):
    return dict(
        TEMPLATE,
        session_id=uuid4().hex,
        org_id=project.organization_id,
        project_id=project.id,
        duration=make_duration(kwargs),
        **kwargs,
    )


class OrganizationSessionsEndpointTest(APITestCase, SnubaTestCase):
    def setUp(self):
        super().setUp()
        self.setup_fixture()

    def setup_fixture(self):
        self.organization1 = self.organization
        self.organization2 = self.create_organization()
        self.organization3 = self.create_organization()
        self.project1 = self.project
        self.project2 = self.create_project()
        self.project3 = self.create_project()
        self.project4 = self.create_project(organization=self.organization2)

        self.user2 = self.create_user(is_superuser=False)
        self.create_member(
            user=self.user2, organization=self.organization1, role="member", teams=[]
        )
        self.create_member(user=self.user, organization=self.organization3, role="admin", teams=[])

        self.create_environment(self.project2, name="development")

        self.store_session(make_session(self.project1, started=SESSION_STARTED + 12 * 60))
        self.store_session(
            make_session(self.project1, started=SESSION_STARTED + 24 * 60, release="foo@1.1.0")
        )
        self.store_session(make_session(self.project1, started=SESSION_STARTED - 60 * 60))
        self.store_session(make_session(self.project1, started=SESSION_STARTED - 12 * 60 * 60))
        self.store_session(make_session(self.project2, status="crashed"))
        self.store_session(make_session(self.project2, environment="development"))
        self.store_session(make_session(self.project3, errors=1, release="foo@1.2.0"))
        self.store_session(
            make_session(
                self.project3,
                distinct_id="39887d89-13b2-4c84-8c23-5d13d2102664",
                started=SESSION_STARTED - 60 * 60,
            )
        )
        self.store_session(
            make_session(
                self.project3, distinct_id="39887d89-13b2-4c84-8c23-5d13d2102664", errors=1
            )
        )
        self.store_session(make_session(self.project4))

    def do_request(self, query, user=None, org=None):
        self.login_as(user=user or self.user)
        url = reverse(
            "sentry-api-0-organization-sessions",
            kwargs={"organization_slug": (org or self.organization).slug},
        )
        return self.client.get(url, query, format="json")

    def test_empty_request(self):
        response = self.do_request({})

        assert response.status_code == 400, response.content
        assert response.data == {"detail": 'Request is missing a "field"'}

    def test_inaccessible_project(self):
        response = self.do_request({"project": [self.project4.id]})

        assert response.status_code == 403, response.content
        assert response.data == {"detail": "You do not have permission to perform this action."}

    def test_unknown_field(self):
        response = self.do_request({"field": ["summ(sessin)"]})

        assert response.status_code == 400, response.content
        assert response.data == {"detail": 'Invalid field: "summ(sessin)"'}

    def test_unknown_groupby(self):
        response = self.do_request({"field": ["sum(session)"], "groupBy": ["envriomnent"]})

        assert response.status_code == 400, response.content
        assert response.data == {"detail": 'Invalid groupBy: "envriomnent"'}

    def test_illegal_groupby(self):
        response = self.do_request({"field": ["sum(session)"], "groupBy": ["issue.id"]})

        assert response.status_code == 400, response.content
        assert response.data == {"detail": 'Invalid groupBy: "issue.id"'}

    def test_invalid_query(self):
        response = self.do_request(
            {"statsPeriod": "1d", "field": ["sum(session)"], "query": ["foo:bar"]}
        )

        assert response.status_code == 400, response.content
        assert response.data == {"detail": 'Invalid query field: "foo"'}

        response = self.do_request(
            {
                "statsPeriod": "1d",
                "field": ["sum(session)"],
                "query": ["release:foo-bar@1.2.3 (123)"],
            }
        )

        assert response.status_code == 400, response.content
        # TODO: it would be good to provide a better error here,
        # since its not obvious where `message` comes from.
        assert response.data == {"detail": 'Invalid query field: "message"'}

    def test_illegal_query(self):
        response = self.do_request(
            {"statsPeriod": "1d", "field": ["sum(session)"], "query": ["issue.id:123"]}
        )
        assert response.status_code == 400, response.content
        assert response.data == {"detail": 'Invalid query field: "group_id"'}

    def test_too_many_points(self):
        # default statsPeriod is 90d
        response = self.do_request({"field": ["sum(session)"], "interval": "1h"})

        assert response.status_code == 400, response.content
        assert response.data == {
            "detail": "Your interval and date range would create too many results. "
            "Use a larger interval, or a smaller date range."
        }

    @freeze_time(MOCK_DATETIME)
    def test_future_request(self):
        start = MOCK_DATETIME + datetime.timedelta(days=1)
        end = MOCK_DATETIME + datetime.timedelta(days=2)
        response = self.do_request(
            {
                "project": [-1],
                "interval": "1h",
                "field": ["sum(session)"],
                "start": start.strftime(SNUBA_TIME_FORMAT),
                "end": end.strftime(SNUBA_TIME_FORMAT),
            }
        )
        assert response.status_code == 200, response.content

    @freeze_time(MOCK_DATETIME)
    def test_timeseries_interval(self):
        response = self.do_request(
            {"project": [-1], "statsPeriod": "1d", "interval": "1d", "field": ["sum(session)"]}
        )

        start_of_day_snuba_format = MOCK_DATETIME_START_OF_DAY.strftime(SNUBA_TIME_FORMAT)

        assert response.status_code == 200, response.content
        assert result_sorted(response.data) == {
            "start": start_of_day_snuba_format,
            "end": MOCK_DATETIME.replace(minute=28, second=0).strftime(SNUBA_TIME_FORMAT),
            "query": "",
            "intervals": [start_of_day_snuba_format],
            "groups": [{"by": {}, "series": {"sum(session)": [9]}, "totals": {"sum(session)": 9}}],
        }

        response = self.do_request(
            {"project": [-1], "statsPeriod": "1d", "interval": "6h", "field": ["sum(session)"]}
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data) == {
            "start": TWO_DAYS_AGO.replace(hour=18, minute=0, second=0).strftime(SNUBA_TIME_FORMAT),
            "end": MOCK_DATETIME.replace(minute=28, second=0).strftime(SNUBA_TIME_FORMAT),
            "query": "",
            "intervals": [
                TWO_DAYS_AGO.replace(hour=18, minute=0, second=0).strftime(SNUBA_TIME_FORMAT),
                MOCK_DATETIME.replace(hour=0, minute=0, second=0).strftime(SNUBA_TIME_FORMAT),
                MOCK_DATETIME.replace(hour=6, minute=0, second=0).strftime(SNUBA_TIME_FORMAT),
                MOCK_DATETIME.replace(hour=12, minute=0, second=0).strftime(SNUBA_TIME_FORMAT),
            ],
            "groups": [
                {"by": {}, "series": {"sum(session)": [0, 1, 2, 6]}, "totals": {"sum(session)": 9}}
            ],
        }

    @freeze_time(MOCK_DATETIME)
    def test_user_all_accessible(self):
        response = self.do_request(
            {"project": [-1], "statsPeriod": "1d", "interval": "1d", "field": ["sum(session)"]},
            user=self.user2,
        )

        start_of_day_snuba_format = MOCK_DATETIME_START_OF_DAY.strftime(SNUBA_TIME_FORMAT)

        assert response.status_code == 200, response.content
        assert result_sorted(response.data) == {
            "start": start_of_day_snuba_format,
            "end": MOCK_DATETIME.replace(hour=12, minute=28, second=0).strftime(SNUBA_TIME_FORMAT),
            "query": "",
            "intervals": [start_of_day_snuba_format],
            "groups": [{"by": {}, "series": {"sum(session)": [9]}, "totals": {"sum(session)": 9}}],
        }

    def test_no_projects(self):
        response = self.do_request(
            {"project": [-1], "statsPeriod": "1d", "interval": "1d", "field": ["sum(session)"]},
            org=self.organization3,
        )

        assert response.status_code == 400, response.content
        assert response.data == {"detail": "No projects available"}

    @freeze_time(MOCK_DATETIME_PLUS_TEN_MINUTES)
    def test_minute_resolution(self):
        with self.feature("organizations:minute-resolution-sessions"):
            response = self.do_request(
                {
                    "project": [self.project1.id, self.project2.id],
                    "statsPeriod": "30m",
                    "interval": "10m",
                    "field": ["sum(session)"],
                }
            )
            assert response.status_code == 200, response.content
            assert result_sorted(response.data) == {
                "start": MOCK_DATETIME.replace(hour=12, minute=0, second=0).strftime(
                    SNUBA_TIME_FORMAT
                ),
                "end": MOCK_DATETIME.replace(hour=12, minute=38, second=0).strftime(
                    SNUBA_TIME_FORMAT
                ),
                "query": "",
                "intervals": [
                    *[
                        MOCK_DATETIME.replace(hour=12, minute=min, second=0).strftime(
                            SNUBA_TIME_FORMAT
                        )
                        for min in [0, 10, 20, 30]
                    ],
                ],
                "groups": [
                    {
                        "by": {},
                        "series": {"sum(session)": [2, 1, 1, 0]},
                        "totals": {"sum(session)": 4},
                    }
                ],
            }

    @freeze_time(MOCK_DATETIME_PLUS_TEN_MINUTES)
    def test_10s_resolution(self):
        with self.feature("organizations:minute-resolution-sessions"):
            response = self.do_request(
                {
                    "project": [self.project1.id],
                    "statsPeriod": "1m",
                    "interval": "10s",
                    "field": ["sum(session)"],
                }
            )
            assert response.status_code == 200, response.content

            from sentry.api.endpoints.organization_sessions import release_health

            if release_health.is_metrics_based():
                # With the metrics backend, we should get exactly what we asked for,
                # 6 intervals with 10 second length. However, because of rounding,
                # we get it rounded to the next minute (see https://github.com/getsentry/sentry/blob/d6c59c32307eee7162301c76b74af419055b9b39/src/sentry/snuba/sessions_v2.py#L388-L392)
                assert len(response.data["intervals"]) == 9
            else:
                # With the sessions backend, the entire period will be aligned
                # to one hour, and the resolution will still be one minute:
                assert len(response.data["intervals"]) == 38

    @freeze_time(MOCK_DATETIME)
    def test_filter_projects(self):
        response = self.do_request(
            {
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "project": [self.project2.id, self.project3.id],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [5]}, "totals": {"sum(session)": 5}}
        ]

    @freeze_time(MOCK_DATETIME)
    def test_filter_environment(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "query": "environment:development",
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [1]}, "totals": {"sum(session)": 1}}
        ]

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "environment": ["development"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [1]}, "totals": {"sum(session)": 1}}
        ]

    @freeze_time(MOCK_DATETIME)
    def test_filter_release(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "query": "release:foo@1.1.0",
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [1]}, "totals": {"sum(session)": 1}}
        ]

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "query": 'release:"foo@1.1.0" or release:"foo@1.2.0"',
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [2]}, "totals": {"sum(session)": 2}}
        ]

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "query": 'release:"foo@1.1.0" or release:["foo@1.2.0", release:"foo@1.3.0"]',
                "groupBy": ["release"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"release": "foo@1.1.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"release": "foo@1.2.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_filter_unknown_release(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1h",
                "field": ["sum(session)"],
                "query": "release:foo@6.6.6",
                "groupBy": "session.status",
            }
        )

        assert response.status_code == 200, response.content

    @freeze_time(MOCK_DATETIME)
    def test_filter_unknown_release_in(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "query": "release:[foo@6.6.6]",
                "groupBy": "session.status",
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"session.status": status},
                "series": {"sum(session)": [0]},
                "totals": {"sum(session)": 0},
            }
            for status in ("abnormal", "crashed", "errored", "healthy")
        ]

    @freeze_time(MOCK_DATETIME)
    def test_groupby_project(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "groupBy": ["project"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"project": self.project1.id},
                "series": {"sum(session)": [4]},
                "totals": {"sum(session)": 4},
            },
            {
                "by": {"project": self.project2.id},
                "series": {"sum(session)": [2]},
                "totals": {"sum(session)": 2},
            },
            {
                "by": {"project": self.project3.id},
                "series": {"sum(session)": [3]},
                "totals": {"sum(session)": 3},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_groupby_environment(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "groupBy": ["environment"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"environment": "development"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"environment": "production"},
                "series": {"sum(session)": [8]},
                "totals": {"sum(session)": 8},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_groupby_release(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "groupBy": ["release"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"release": "foo@1.0.0"},
                "series": {"sum(session)": [7]},
                "totals": {"sum(session)": 7},
            },
            {
                "by": {"release": "foo@1.1.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"release": "foo@1.2.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_groupby_status(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "groupBy": ["session.status"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"session.status": "abnormal"},
                "series": {"sum(session)": [0]},
                "totals": {"sum(session)": 0},
            },
            {
                "by": {"session.status": "crashed"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"session.status": "errored"},
                "series": {"sum(session)": [2]},
                "totals": {"sum(session)": 2},
            },
            {
                "by": {"session.status": "healthy"},
                "series": {"sum(session)": [6]},
                "totals": {"sum(session)": 6},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_groupby_cross(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "groupBy": ["release", "environment"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"environment": "development", "release": "foo@1.0.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"environment": "production", "release": "foo@1.0.0"},
                "series": {"sum(session)": [6]},
                "totals": {"sum(session)": 6},
            },
            {
                "by": {"environment": "production", "release": "foo@1.1.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"environment": "production", "release": "foo@1.2.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_users_groupby(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["count_unique(user)"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"count_unique(user)": [1]}, "totals": {"count_unique(user)": 1}}
        ]

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["count_unique(user)"],
                "groupBy": ["session.status"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"session.status": "abnormal"},
                "series": {"count_unique(user)": [0]},
                "totals": {"count_unique(user)": 0},
            },
            {
                "by": {"session.status": "crashed"},
                "series": {"count_unique(user)": [0]},
                "totals": {"count_unique(user)": 0},
            },
            {
                "by": {"session.status": "errored"},
                "series": {"count_unique(user)": [1]},
                "totals": {"count_unique(user)": 1},
            },
            {
                "by": {"session.status": "healthy"},
                "series": {"count_unique(user)": [0]},
                "totals": {"count_unique(user)": 0},
            },
        ]

    expected_duration_values = {
        "avg(session.duration)": 42375.0,
        "max(session.duration)": 80000.0,
        "p50(session.duration)": 33500.0,
        "p75(session.duration)": 53750.0,
        "p90(session.duration)": 71600.0,
        "p95(session.duration)": 75800.0,
        "p99(session.duration)": 79159.99999999999,
    }

    @freeze_time(MOCK_DATETIME)
    def test_duration_percentiles(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": [
                    "avg(session.duration)",
                    "p50(session.duration)",
                    "p75(session.duration)",
                    "p90(session.duration)",
                    "p95(session.duration)",
                    "p99(session.duration)",
                    "max(session.duration)",
                ],
            }
        )

        assert response.status_code == 200, response.content

        expected = self.expected_duration_values

        groups = result_sorted(response.data)["groups"]
        assert len(groups) == 1, groups
        group = groups[0]
        assert group["by"] == {}

        assert group["totals"] == pytest.approx(expected)
        for key, series in group["series"].items():
            assert series == pytest.approx([expected[key]])

    @freeze_time(MOCK_DATETIME)
    def test_duration_percentiles_groupby(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": [
                    "avg(session.duration)",
                    "p50(session.duration)",
                    "p75(session.duration)",
                    "p90(session.duration)",
                    "p95(session.duration)",
                    "p99(session.duration)",
                    "max(session.duration)",
                ],
                "groupBy": "session.status",
            }
        )

        assert response.status_code == 200, response.content

        expected = self.expected_duration_values

        seen = set()  # Make sure all session statuses are listed
        for group in result_sorted(response.data)["groups"]:
            seen.add(group["by"].get("session.status"))

            if group["by"] == {"session.status": "healthy"}:
                assert group["totals"] == pytest.approx(expected)
                for key, series in group["series"].items():
                    assert series == pytest.approx([expected[key]])
            else:
                # Everything's none:
                assert group["totals"] == {key: None for key in expected}, group["by"]
                assert group["series"] == {key: [None] for key in expected}

        assert seen == {"abnormal", "crashed", "errored", "healthy"}

    @freeze_time(MOCK_DATETIME)
    def test_snuba_limit_exceeded(self):
        # 2 * 3 => only show two groups
        with patch("sentry.snuba.sessions_v2.SNUBA_LIMIT", 6), patch(
            "sentry.snuba.metrics.query.MAX_POINTS", 6
        ):

            response = self.do_request(
                {
                    "project": [-1],
                    "statsPeriod": "3d",
                    "interval": "1d",
                    "field": ["sum(session)", "count_unique(user)"],
                    "groupBy": ["project", "release", "environment"],
                }
            )

            assert response.status_code == 200, response.content
            assert result_sorted(response.data)["groups"] == [
                {
                    "by": {
                        "release": "foo@1.0.0",
                        "environment": "production",
                        "project": self.project1.id,
                    },
                    "totals": {"sum(session)": 3, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 3], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "release": "foo@1.0.0",
                        "environment": "production",
                        "project": self.project3.id,
                    },
                    "totals": {"sum(session)": 2, "count_unique(user)": 1},
                    "series": {"sum(session)": [0, 0, 2], "count_unique(user)": [0, 0, 1]},
                },
            ]

    @freeze_time(MOCK_DATETIME)
    def test_snuba_limit_exceeded_groupby_status(self):
        """Get consistent result when grouping by status"""
        # 2 * 3 => only show two groups
        with patch("sentry.snuba.sessions_v2.SNUBA_LIMIT", 6), patch(
            "sentry.snuba.metrics.query.MAX_POINTS", 6
        ):

            response = self.do_request(
                {
                    "project": [-1],
                    "statsPeriod": "3d",
                    "interval": "1d",
                    "field": ["sum(session)", "count_unique(user)"],
                    "groupBy": ["project", "release", "environment", "session.status"],
                }
            )

            assert response.status_code == 200, response.content
            assert result_sorted(response.data)["groups"] == [
                {
                    "by": {
                        "project": self.project1.id,
                        "release": "foo@1.0.0",
                        "session.status": "abnormal",
                        "environment": "production",
                    },
                    "totals": {"sum(session)": 0, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 0], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "project": self.project1.id,
                        "release": "foo@1.0.0",
                        "session.status": "crashed",
                        "environment": "production",
                    },
                    "totals": {"sum(session)": 0, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 0], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "project": self.project1.id,
                        "release": "foo@1.0.0",
                        "environment": "production",
                        "session.status": "errored",
                    },
                    "totals": {"sum(session)": 0, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 0], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "project": self.project1.id,
                        "session.status": "healthy",
                        "release": "foo@1.0.0",
                        "environment": "production",
                    },
                    "totals": {"sum(session)": 3, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 3], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "session.status": "abnormal",
                        "release": "foo@1.0.0",
                        "project": self.project3.id,
                        "environment": "production",
                    },
                    "totals": {"sum(session)": 0, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 0], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "release": "foo@1.0.0",
                        "project": self.project3.id,
                        "session.status": "crashed",
                        "environment": "production",
                    },
                    "totals": {"sum(session)": 0, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 0], "count_unique(user)": [0, 0, 0]},
                },
                {
                    "by": {
                        "release": "foo@1.0.0",
                        "project": self.project3.id,
                        "environment": "production",
                        "session.status": "errored",
                    },
                    "totals": {"sum(session)": 1, "count_unique(user)": 1},
                    "series": {"sum(session)": [0, 0, 1], "count_unique(user)": [0, 0, 1]},
                },
                {
                    "by": {
                        "session.status": "healthy",
                        "release": "foo@1.0.0",
                        "project": self.project3.id,
                        "environment": "production",
                    },
                    "totals": {"sum(session)": 1, "count_unique(user)": 0},
                    "series": {"sum(session)": [0, 0, 1], "count_unique(user)": [0, 0, 0]},
                },
            ]

    @freeze_time(MOCK_DATETIME)
    def test_environment_filter_not_present_in_query(self):
        self.create_environment(name="abc")
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["sum(session)"],
                "environment": ["development", "abc"],
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [1]}, "totals": {"sum(session)": 1}}
        ]

    @freeze_time(MOCK_DATETIME)
    def test_sessions_without_users(self):
        # The first field defines by which groups additional queries are filtered
        # But if the first field is the user count, the series should still
        # contain the session counts even if the project does not track users
        response = self.do_request(
            {
                "project": self.project.id,  # project without users
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["count_unique(user)", "sum(session)"],
                "groupBy": "release",
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"release": "foo@1.0.0"},
                "series": {"count_unique(user)": [0], "sum(session)": [3]},
                "totals": {"count_unique(user)": 0, "sum(session)": 3},
            },
            {
                "by": {"release": "foo@1.1.0"},
                "series": {"count_unique(user)": [0], "sum(session)": [1]},
                "totals": {"count_unique(user)": 0, "sum(session)": 1},
            },
        ]

    @freeze_time(MOCK_DATETIME + datetime.timedelta(days=2))
    def test_groupby_no_data(self):
        # Empty results for everything
        response = self.do_request(
            {
                "project": self.project.id,  # project without users
                "statsPeriod": "1d",
                "interval": "1d",
                "field": ["count_unique(user)", "sum(session)"],
                "groupBy": "release",
            }
        )

        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == []

    @freeze_time(MOCK_DATETIME)
    def test_mix_known_and_unknown_strings(self):
        for query_string in ("environment:[production,foo]",):
            response = self.do_request(
                {
                    "project": self.project.id,  # project without users
                    "statsPeriod": "1d",
                    "interval": "1d",
                    "field": ["count_unique(user)", "sum(session)"],
                    "query": query_string,
                }
            )
            assert response.status_code == 200, response.data


@patch("sentry.api.endpoints.organization_sessions.release_health", MetricsReleaseHealthBackend())
class OrganizationSessionsEndpointMetricsTest(
    SessionMetricsTestCase, OrganizationSessionsEndpointTest
):
    """Repeat all tests with metrics backend"""

    @freeze_time(MOCK_DATETIME)
    def test_orderby(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)"],
                "orderBy": "foobar",
            }
        )
        assert response.status_code == 400
        assert response.data == {"detail": "'orderBy' must be one of the provided 'fields'"}

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)"],
                "orderBy": "count_unique(user)",  # wrong field
            }
        )
        assert response.status_code == 400
        assert response.data == {"detail": "'orderBy' must be one of the provided 'fields'"}

        # Cannot sort by more than one field
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)", "count_unique(user)"],
                "orderBy": ["sum(session)", "count_unique(user)"],
            }
        )
        assert response.status_code == 400
        assert response.data == {"detail": "Cannot order by multiple fields"}

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)"],
                "orderBy": "sum(session)",  # misses group by, but why not
            }
        )
        assert response.status_code == 200

        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)"],
                "orderBy": "sum(session)",
                "groupBy": ["session.status"],
            }
        )
        assert response.status_code == 400
        assert response.data == {"detail": "Cannot use 'orderBy' when grouping by sessions.status"}

        response = self.do_request(
            {
                "project": [self.project.id, self.project3.id],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)", "p95(session.duration)"],
                "orderBy": "p95(session.duration)",
                "groupBy": ["project", "release", "environment"],
            }
        )

        expected_groups = [
            {
                "by": {
                    "project": self.project.id,
                    "release": "foo@1.0.0",
                    "environment": "production",
                },
                "totals": {"sum(session)": 3, "p95(session.duration)": 25000.0},
                "series": {"sum(session)": [0, 3], "p95(session.duration)": [None, 25000.0]},
            },
            {
                "by": {
                    "project": self.project3.id,
                    "release": "foo@1.2.0",
                    "environment": "production",
                },
                "totals": {"sum(session)": 1, "p95(session.duration)": 37000.0},
                "series": {"sum(session)": [0, 1], "p95(session.duration)": [None, 37000.0]},
            },
            {
                "by": {
                    "project": self.project.id,
                    "release": "foo@1.1.0",
                    "environment": "production",
                },
                "totals": {"sum(session)": 1, "p95(session.duration)": 49000.0},
                "series": {"sum(session)": [0, 1], "p95(session.duration)": [None, 49000.0]},
            },
            {
                "by": {
                    "project": self.project3.id,
                    "release": "foo@1.0.0",
                    "environment": "production",
                },
                "totals": {"sum(session)": 2, "p95(session.duration)": 79400.0},
                "series": {"sum(session)": [0, 2], "p95(session.duration)": [None, 79400.0]},
            },
        ]

        # Not using `result_sorted` here, because we want to verify the order
        assert response.status_code == 200, response.data
        assert response.data["groups"] == expected_groups

        # Sort descending
        response = self.do_request(
            {
                "project": [self.project.id, self.project3.id],
                "statsPeriod": "2d",
                "interval": "1d",
                "field": ["sum(session)", "p95(session.duration)"],
                "orderBy": "-p95(session.duration)",
                "groupBy": ["project", "release", "environment"],
            }
        )

        assert response.status_code == 200
        assert response.data["groups"] == list(reversed(expected_groups))

        # Add some more code coverage
        all_fields = [
            "sum(session)",
            "count_unique(user)",
            "avg(session.duration)",
        ]
        for field in all_fields:
            assert (
                self.do_request(
                    {
                        "project": [self.project.id, self.project3.id],
                        "statsPeriod": "2d",
                        "interval": "1d",
                        "field": all_fields,
                        "orderBy": field,
                        "groupBy": ["project", "release", "environment"],
                    }
                ).status_code
                == 200
            )

    @freeze_time(MOCK_DATETIME)
    def test_filter_by_session_status(self):
        default_request = {
            "project": [-1],
            "statsPeriod": "1d",
            "interval": "1d",
        }

        def req(**kwargs):
            return self.do_request(dict(default_request, **kwargs))

        response = req(field=["sum(session)"], query="session.status:bogus")
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == []

        response = req(field=["sum(session)"], query="!session.status:healthy")
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [3]}, "totals": {"sum(session)": 3}}
        ]

        # sum(session) filtered by multiple statuses adds them
        response = req(field=["sum(session)"], query="session.status:[healthy, errored]")
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [8]}, "totals": {"sum(session)": 8}}
        ]

        response = req(
            field=["sum(session)"],
            query="session.status:[healthy, errored]",
            groupBy="session.status",
        )
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"session.status": "errored"},
                "totals": {"sum(session)": 2},
                "series": {"sum(session)": [2]},
            },
            {
                "by": {"session.status": "healthy"},
                "totals": {"sum(session)": 6},
                "series": {"sum(session)": [6]},
            },
        ]

        response = req(field=["sum(session)"], query="session.status:healthy release:foo@1.1.0")
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {"by": {}, "series": {"sum(session)": [1]}, "totals": {"sum(session)": 1}}
        ]

        response = req(field=["sum(session)"], query="session.status:healthy OR release:foo@1.1.0")
        assert response.status_code == 400, response.data
        assert response.data == {"detail": "Unable to parse condition with session.status"}

        # count_unique(user) does not work with multiple session statuses selected
        response = req(field=["count_unique(user)"], query="session.status:[healthy, errored]")
        assert response.status_code == 400, response.data
        assert response.data == {
            "detail": "Cannot filter count_unique by multiple session.status unless it is in groupBy"
        }

        response = req(field=["p95(session.duration)"], query="session.status:abnormal")
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == []

    @freeze_time(MOCK_DATETIME)
    def test_filter_by_session_status_with_groupby(self):
        default_request = {
            "project": [-1],
            "statsPeriod": "1d",
            "interval": "1d",
            "groupBy": "release",
        }

        def req(**kwargs):
            return self.do_request(dict(default_request, **kwargs))

        response = req(field=["sum(session)"], query="session.status:healthy")
        assert response.status_code == 200, response.content
        assert result_sorted(response.data)["groups"] == [
            {
                "by": {"release": "foo@1.0.0"},
                "series": {"sum(session)": [5]},
                "totals": {"sum(session)": 5},
            },
            {
                "by": {"release": "foo@1.1.0"},
                "series": {"sum(session)": [1]},
                "totals": {"sum(session)": 1},
            },
            {
                "by": {"release": "foo@1.2.0"},
                "series": {"sum(session)": [0]},
                "totals": {"sum(session)": 0},
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_filter_by_session_status_with_orderby(self):
        default_request = {
            "project": [-1],
            "statsPeriod": "1d",
            "interval": "1d",
        }

        def req(**kwargs):
            return self.do_request(dict(default_request, **kwargs))

        response = req(
            field=["sum(session)"],
            query="session.status:[abnormal,crashed]",
            groupBy="release",
            orderBy="sum(session)",
        )
        assert response.status_code == 400, response.content
        assert response.data == {"detail": "Cannot order by sum(session) with the current filters"}

        response = req(
            field=["sum(session)"],
            query="session.status:healthy",
            groupBy="release",
            orderBy="sum(session)",
        )
        assert response.status_code == 400, response.content
        assert response.data == {"detail": "Cannot order by sum(session) with the current filters"}

    @freeze_time(MOCK_DATETIME)
    def test_crash_rate(self):
        default_request = {
            "project": [-1],
            "statsPeriod": "1d",
            "interval": "1d",
            "field": ["crash_rate(session)"],
        }

        def req(**kwargs):
            return self.do_request(dict(default_request, **kwargs))

        # 1 - filter session.status
        response = req(
            query="session.status:[abnormal,crashed]",
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            "detail": "Cannot filter field crash_rate(session) by session.status"
        }

        # 2 - group by session.status
        response = req(
            groupBy="session.status",
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            "detail": "Cannot group field crash_rate(session) by session.status"
        }

        # 4 - fetch all
        response = req(
            field=[
                "crash_rate(session)",
                "crash_rate(user)",
                "crash_free_rate(session)",
                "crash_free_rate(user)",
            ],
            groupBy=["release", "environment"],
            orderBy=["crash_free_rate(session)"],
            query="release:foo@1.0.0",
        )
        assert response.status_code == 200, response.content
        assert response.data["groups"] == [
            {
                "by": {"environment": "production", "release": "foo@1.0.0"},
                "series": {
                    "crash_free_rate(session)": [0.8333333333333334],
                    "crash_free_rate(user)": [1.0],
                    "crash_rate(session)": [0.16666666666666666],
                    "crash_rate(user)": [0.0],
                },
                "totals": {
                    "crash_free_rate(session)": 0.8333333333333334,
                    "crash_free_rate(user)": 1.0,
                    "crash_rate(session)": 0.16666666666666666,
                    "crash_rate(user)": 0.0,
                },
            },
            {
                "by": {"environment": "development", "release": "foo@1.0.0"},
                "series": {
                    "crash_free_rate(session)": [1.0],
                    "crash_free_rate(user)": [None],
                    "crash_rate(session)": [0.0],
                    "crash_rate(user)": [None],
                },
                "totals": {
                    "crash_free_rate(session)": 1.0,
                    "crash_free_rate(user)": None,
                    "crash_rate(session)": 0.0,
                    "crash_rate(user)": None,
                },
            },
        ]

    @freeze_time(MOCK_DATETIME)
    def test_pagination(self):
        def do_request(cursor):
            return self.do_request(
                {
                    "project": self.project.id,  # project without users
                    "statsPeriod": "1d",
                    "interval": "1d",
                    "field": ["count_unique(user)", "sum(session)"],
                    "query": "",
                    "groupBy": "release",
                    "orderBy": "sum(session)",
                    "per_page": 1,
                    **({"cursor": cursor} if cursor else {}),
                }
            )

        response = do_request(None)

        assert response.status_code == 200, response.data
        assert len(response.data["groups"]) == 1
        assert response.data["groups"] == [
            {
                "by": {"release": "foo@1.1.0"},
                "series": {"count_unique(user)": [0], "sum(session)": [1]},
                "totals": {"count_unique(user)": 0, "sum(session)": 1},
            }
        ]
        links = {link["rel"]: link for url, link in parse_link_header(response["Link"]).items()}
        assert links["previous"]["results"] == "false"
        assert links["next"]["results"] == "true"

        response = do_request(links["next"]["cursor"])
        assert response.status_code == 200, response.data
        assert len(response.data["groups"]) == 1
        assert response.data["groups"] == [
            {
                "by": {"release": "foo@1.0.0"},
                "series": {"count_unique(user)": [0], "sum(session)": [3]},
                "totals": {"count_unique(user)": 0, "sum(session)": 3},
            }
        ]
        links = {link["rel"]: link for url, link in parse_link_header(response["Link"]).items()}
        assert links["previous"]["results"] == "true"
        assert links["next"]["results"] == "false"

    def test_unrestricted_date_range(self):
        response = self.do_request(
            {
                "project": [-1],
                "statsPeriod": "7h",
                "interval": "5m",
                "field": ["sum(session)"],
            }
        )
        assert response.status_code == 200

    @freeze_time(MOCK_DATETIME)
    def test_release_is_empty(self):
        self.store_session(
            make_session(
                self.project1, started=SESSION_STARTED + 12 * 60, release="", environment=""
            )
        )
        for query in ('release:"" environment:""', 'release:"" OR environment:""'):
            # Empty strings are invalid values for releases and environments, but we should still handle those cases correctly at the query layer
            response = self.do_request(
                {
                    "project": self.project.id,  # project without users
                    "statsPeriod": "1d",
                    "interval": "1d",
                    "field": ["sum(session)"],
                    "query": query,
                    "groupBy": ["release", "environment"],
                }
            )

            assert response.status_code == 200, response.content
            assert result_sorted(response.data)["groups"] == [
                {
                    "by": {"environment": "", "release": ""},
                    "series": {"sum(session)": [1]},
                    "totals": {"sum(session)": 1},
                }
            ]
