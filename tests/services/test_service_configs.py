"""recognition and event_processing configs load a .env file (like detection);
environment variables take priority over it."""
import pytest

from services.event_processing.src.workers.headcount.config import HeadcountConfig
from services.event_processing.src.workers.intruder.config import IntruderConfig
from services.event_processing.src.workers.zone_monitor.config import ZoneMonitorConfig
from services.recognition.src.config import RecognitionConfig

# (class, env var, attribute, value in .env, parsed value)
CASES = [
    (RecognitionConfig, "ENROLLED_THRESHOLD", "ENROLLED_THRESHOLD", "0.81", 0.81),
    (RecognitionConfig, "RECOGNITION_MODEL_PACK", "RECOGNITION_MODEL_PACK", "buffalo_l", "buffalo_l"),
    (RecognitionConfig, "RECOGNITION_CONSUMER_NAME", "CONSUMER_NAME", "rec_7", "rec_7"),
    (RecognitionConfig, "RECOGNITION_FACE_AMBIGUITY_RATIO", "FACE_AMBIGUITY_RATIO", "1.5", 1.5),
    (ZoneMonitorConfig, "LOST_TRACK_TIMEOUT_S", "LOST_TRACK_TIMEOUT_S", "3.5", 3.5),
    (ZoneMonitorConfig, "ZONE_SWEEP_INTERVAL_S", "SWEEP_INTERVAL_S", "7", 7.0),
    (IntruderConfig, "RECOGNITION_MAX_AGE_S", "RECOGNITION_MAX_AGE_S", "12", 12.0),
    (IntruderConfig, "INTRUDER_RECOGNITION_LOOKUP_RETRIES", "RECOGNITION_LOOKUP_RETRIES", "2", 2),
    (HeadcountConfig, "HEADCOUNT_ROLLING_WINDOW_SECONDS", "ROLLING_WINDOW_SECONDS", "10", 10.0),
    (HeadcountConfig, "HEADCOUNT_BREACH_EXIT_SECONDS", "BREACH_EXIT_SECONDS", "5", 5.0),
    (HeadcountConfig, "DATABASE_URL", "DATABASE_URL", "postgresql+asyncpg://a:b@h/db", "postgresql+asyncpg://a:b@h/db"),
]
IDS = [f"{c.__name__}-{env}" for c, env, *_ in CASES]


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for _, env, *_ in CASES:
        monkeypatch.delenv(env, raising=False)
    return tmp_path


@pytest.mark.parametrize("cls,env,attr,raw,value", CASES, ids=IDS)
def test_value_is_read_from_dotenv(workdir, cls, env, attr, raw, value):
    (workdir / ".env").write_text(f"{env}={raw}\nUNRELATED_SECRET=x\n")
    assert getattr(cls(), attr) == value


@pytest.mark.parametrize("cls,env,attr,raw,value", CASES, ids=IDS)
def test_environment_wins_over_dotenv(workdir, monkeypatch, cls, env, attr, raw, value):
    (workdir / ".env").write_text(f"{env}={raw}\n")
    default = getattr(cls.model_fields[attr], "default", None)
    monkeypatch.setenv(env, str(default) if default is not None else "from-env")
    assert getattr(cls(), attr) != value


def test_defaults_without_dotenv_or_env(workdir):
    assert RecognitionConfig().ENROLLED_THRESHOLD == 0.75
    assert HeadcountConfig().ROLLING_WINDOW_SECONDS == 30.0
    assert IntruderConfig().RECOGNITION_LOOKUP_DELAY_SECONDS == 0.3
    assert ZoneMonitorConfig().LOST_TRACK_TIMEOUT_S == 2.0


def test_stream_and_group_names_are_not_configurable(workdir, monkeypatch):
    monkeypatch.setenv("DETECTIONS_STREAM", "other")
    monkeypatch.setenv("CONSUMER_GROUP", "other")
    assert RecognitionConfig().DETECTIONS_STREAM == "events:detections"
    assert RecognitionConfig().CONSUMER_GROUP == "recognition_group"
    assert HeadcountConfig().CONSUMER_GROUP == "headcount_group"
