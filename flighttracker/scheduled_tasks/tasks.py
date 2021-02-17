import logging
import sys
import time
from contextlib import closing
from typing import Dict, List

from celery.app.log import TaskFormatter
from celery.signals import after_setup_task_logger
from celery.utils.log import get_task_logger
from psycopg2 import DatabaseError

from flighttracker.config import common_conf
from flighttracker.database import DB
from flighttracker.opensky_api import OpenskyStates
from flighttracker.scheduled_tasks.celery import app

StateVector = Dict
StateVectors = List[StateVector]

logger = get_task_logger(__name__)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@after_setup_task_logger.connect
def setup_task_logger(logger, **kwargs):
    for handler in logger.handlers:
        handler.setFormatter(
            TaskFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )


def insert_state_vectors_to_db(cur_time: int) -> None:
    api = OpenskyStates()
    try:
        with closing(DB(**common_conf.db_params)) as db:
            logger.info(f"A request has been sent to API for the timestamp: {cur_time}")
            states: StateVectors = api.get_current_states(time_sec=cur_time)
            logger.info(
                f"A response has been received from API for the timestamp: {cur_time}"
            )

            state_vectors_quantity = 0
            for _ in states:
                state_vectors_quantity += 1

            resp_time: int = states[0]["request_time"]
            logger.info(
                f"Received {state_vectors_quantity} state vectors for the timestamp {resp_time}:"
            )
            logger.info("Insertion to DB has started")
            db.insert_current_states(states)
            logger.info("Insertion to DB has finished")
            logger.debug(f"Inserted state vectors: {states}")

    except (Exception, DatabaseError) as e:
        logger.exception(f"Exception in main(): {e}")
        insert_state_vectors_to_db(cur_time=cur_time)


def update_flight_paths():
    try:
        with closing(DB(**common_conf.db_params)) as db:
            logger.info("Starting flight paths update")
            db.update_paths()
    except (Exception, DatabaseError) as e:
        logger.exception(f"Exception in main(): {e}")
        update_paths()


def update_airport_stats():
    try:
        with closing(DB(**common_conf.db_params)) as db:
            logger.info("Starting airports statistics update")
            db.update_airport_stats()
    except (Exception, DatabaseError) as e:
        logger.exception(f"Exception in main(): {e}")
        update_airport_stats()


@app.task(bind=True)
def insert_states(self):
    self.time_limit = 15
    cur_time = int(time.time())
    insert_state_vectors_to_db(cur_time)
    old_outs = sys.stdout, sys.stderr
    rlevel = app.conf.worker_redirect_stdouts_level
    try:
        app.log.redirect_stdouts_to_logger(logger, rlevel)
    finally:
        sys.stdout, sys.stderr = old_outs


@app.task(bind=True)
def update_paths(self):
    self.time_limit = 10
    update_flight_paths()


@app.task(bind=True)
def update_stats(self):
    self.time_limit = 10
    update_airport_stats()
