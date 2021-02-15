import json
from datetime import date
import time
from typing import List, Dict, Tuple
import logging
import os

import yaml
from psycopg2 import connect

from flighttracker.opensky_api import OpenskyStates
from flighttracker.scripts import airports_insertion

State_vector = Dict
State_vectors = List[State_vector]

logger = logging.getLogger()


class DB:
    def __init__(self, dbname, user, password, host, port):
        logger.info('')
        logger.info('Connecting to the PostgreSQL database...')
        self.conn = connect(dbname=dbname, user=user, password=password, host=host, port=port)
        check = (
            '''
                SELECT 1;
            '''
        )
        with self.conn:
            with self.conn.cursor() as curs:
                curs.execute(check)
        logger.info('Successful connection to the PostgreSQL database')

    def make_table(self, table_name):
        script_dir = os.path.abspath(__file__ + '/../')
        rel_path = 'make_table_scripts.yaml'
        path = os.path.join(script_dir, rel_path)
        with open(path, 'r') as tm:
            script = yaml.load(tm, Loader=yaml.FullLoader)[table_name]['create']
        with self.conn:
            with self.conn.cursor() as curs:
                curs.execute(script)

    def upsert_state_vectors(self, vectors: State_vectors) -> None:
        with self.conn:
            with self.conn.cursor() as curs:
                curs.execute(
                    '''
                        SELECT EXISTS (SELECT * FROM information_schema.tables WHERE table_name=%s)
                    ''', ('current_states',)
                )
                table_exists = curs.fetchone()[0]
                if table_exists is not True:
                    self.make_table('current_states')

                for vector in vectors:
                    state_vector = {
                        'request_time': vector['request_time'],
                        'icao24': vector['icao24'],
                        'callsign': vector['callsign'],
                        'origin_country': vector['origin_country'],
                        'time_position': vector['time_position'],
                        'last_contact': vector['last_contact'],
                        'longitude': vector['longitude'],
                        'latitude': vector['latitude'],
                        'baro_altitude': vector['baro_altitude'],
                        'on_ground': vector['on_ground'],
                        'velocity': vector['velocity'],
                        'true_track': vector['true_track'],
                        'vertical_rate': vector['vertical_rate'],
                        'sensors': vector['sensors'],
                        'geo_altitude': vector['geo_altitude'],
                        'squawk': vector['squawk'],
                        'spi': vector['spi'],
                        'position_source': vector['position_source'],
                    }
                    curs.execute(
                        '''
                            DELETE FROM current_states;
                            
                            INSERT INTO current_states (request_time, icao24, callsign, origin_country, 
                            time_position, last_contact, longitude, latitude, baro_altitude, on_ground, velocity, 
                            true_track, vertical_rate, sensors, geo_altitude, squawk, spi, position_source)
                            VALUES (%(request_time)s, %(icao24)s, %(callsign)s, %(origin_country)s, %(time_position)s,
                            %(last_contact)s, %(longitude)s, %(latitude)s, %(baro_altitude)s, %(on_ground)s, %(velocity)s,
                            %(true_track)s, %(vertical_rate)s, %(sensors)s, %(geo_altitude)s, %(squawk)s, %(spi)s,
                            %(position_source)s)
                            ON CONFLICT (request_time, icao24) 
                            DO UPDATE SET (time_position, last_contact, longitude, latitude, baro_altitude, on_ground, 
                            velocity, true_track, vertical_rate, geo_altitude) = (%(time_position)s, %(last_contact)s, 
                            %(longitude)s, %(latitude)s, %(baro_altitude)s, %(on_ground)s, %(velocity)s, 
                            %(true_track)s, %(vertical_rate)s, %(geo_altitude)s);
                        ''',
                        state_vector,
                    )

    def get_last_inserted_state(self) -> Tuple[State_vectors, int]:
        with self.conn.cursor() as curs:
            curs.execute(
                '''
                    SELECT * FROM current_states
                    WHERE request_time = (SELECT MAX(request_time) FROM current_states);
                '''
            )
            vectors = curs.fetchall()

            state_vectors_quantity = 0
            for _ in vectors:
                state_vectors_quantity += 1

            return vectors, state_vectors_quantity

    def insert_new_path(self, state, flight_info):
        with self.conn.cursor() as curs:
            curs.execute(
                '''
                    SELECT EXISTS (SELECT * FROM information_schema.tables WHERE table_name=%s)
                ''', ('airports',)
            )
            table_exists = curs.fetchone()[0]
            if table_exists is not True:
                self.make_table('airports')

                script_dir = os.path.abspath(__file__ + '/../../')
                rel_path = 'extra/airports.rtf'
                path = os.path.join(script_dir, rel_path)

                airports_insertion.fill_airports(path)

        last_update = state[0]
        icao24 = state[1]
        first_location = {
            'longitude': state[6],
            'latitude': state[7]
        }
        path = [json.dumps(first_location)]
        finished = False

        flight_info['last_update'] = last_update
        flight_info['icao24'] = icao24
        flight_info['path'] = path
        flight_info['finished'] = finished

        with self.conn.cursor() as curs:
            curs.execute(
                '''
                    INSERT INTO flight_paths (last_update, icao24, departure_airport_icao, arrival_airport_icao,
                    arrival_airport_long, arrival_airport_lat, estimated_arrival_time, path, finished)
                    VALUES (%(last_update)s, %(icao24)s, %(departure_airport_icao)s, %(arrival_airport_icao)s,
                    %(arrival_airport_long)s, %(arrival_airport_lat)s, %(estimated_arrival_time)s, %(path)s, %(finished)s)
                ''', flight_info
            )

    def update_paths(self):
        with self.conn.cursor() as curs:
            curs.execute(
                '''
                    SELECT EXISTS (SELECT * FROM information_schema.tables WHERE table_name=%s)
                ''', ('flight_paths',)
            )
            table_exists = curs.fetchone()[0]
            if table_exists is not True:
                self.make_table('flight_paths')

            curs.execute(
                '''
                    SELECT * FROM current_states;
                '''
            )
            states = curs.fetchall()
            if len(states) > 0:
                time = states[0]

                api = OpenskyStates()
                airports_response = api.get_airports(begin=time - 5, end=time + 5)

                for state in states:
                    curs.execute(
                        '''
                            SELECT * FROM flight_paths
                            WHERE icao24 = state[1];
                        '''
                    )
                    paths = curs.fetchall()

                    for item in airports_response:
                        if item[0] == state[1]:
                            departure_airport_icao = item[1]
                            arrival_airport_icao = item[2]
                            estimated_arr_time = item[3]
                            airport_icao = '"' + arrival_airport_icao + '"'
                            curs.execute(
                                '''
                                    SELECT latitude, longitude FROM airports
                                    WHERE icao = airport_icao;
                                '''
                            )
                            airport = curs.fetchone()
                            arrival_airport_lat = airport[0]
                            arrival_airport_long = airport[1]
                            break

                    flight_info = {
                        'departure_airport_icao': departure_airport_icao,
                        'arrival_airport_icao': arrival_airport_icao,
                        'arrival_airport_long': arrival_airport_long,
                        'arrival_airport_lat': arrival_airport_lat,
                        'estimated_arrival_time': estimated_arr_time
                    }

                    if len(paths) == 0:
                        self.insert_new_path(state, flight_info)
                    else:
                        latest_update = 0
                        max_ind = 0
                        for i in range(0, len(paths)):
                            if paths[i][1] > latest_update:
                                latest_update = paths[i][1]
                                max_ind = i

                        if paths[max_ind][9] is True:
                            self.insert_new_path(state, flight_info)
                        else:
                            current_location = {
                                'longitude': state[6],
                                'latitude': state[7]
                            }
                            add_path = [json.dumps(current_location)]

                            update_sql = '''
                                UPDATE flight_paths
                                SET path = path || %s::jsonb
                                WHERE icao24 = state[1] AND last_update = latest_update
                            '''
                            curs.execute(update_sql, add_path)

    def update_airport_stats(self):
        with self.conn.cursor() as curs:
            curs.execute(
                '''
                    SELECT EXISTS (SELECT * FROM information_schema.tables WHERE table_name=%s)
                '''
                , ('airport_stats',))
            table_exists = curs.fetchone()[0]
            if table_exists is not True:
                self.make_table('airport_stats')

            today = date.today()
            today_date = today.strftime('%b-%d-%Y')

            time_now = time.time()
            logger.info(f'time now = {time_now}')

            curs.execute(
                '''
                    SELECT * FROM flight_paths
                    WHERE ((%s) - last_update) < 3600 AND finished = True
                ''', (time_now,)
            )

            paths = curs.fetchall()
            counts = {}
            for path in paths:
                arr_airport = path[4]
                dep_airport = path[3]

                curs.execute(
                    '''
                       SELECT * FROM airport_stats 
                       WHERE airport_icao = arr_airport AND date = today_date;
                    '''
                )
                stats = curs.fetchall()

                if len(stats) == 0:
                    info = {
                        'airport_icao': arr_airport,
                        'date_today': today_date,
                        'airplane_quantity_arrivals': 1,
                        'airplane_quantity_departures': 0
                    }
                    curs.execute(
                        '''
                            INSERT INTO airport_stats (airport_icao, date, airplane_quantity_arrivals, airplane_quantity_departures)
                            VALUES (%(airport_icao)s, %(date_today)s, %(airplane_quantity_arrivals)s, %(airplane_quantity_departures)s)
                        ''', info
                    )
                else:
                    curs.execute(
                        '''
                            UPDATE airport_stats
                            SET airplane_quantity_arrivals = airplane_quantity_arrivals + 1
                            WHERE airport_icao = (%s) AND date_today = (%s)
                        ''', (stats[0][1], stats[0][2])
                    )

                curs.execute(
                    '''
                        SELECT * FROM airport_stats
                        WHERE airport_icao = dep_airport AND date = today_date;
                    '''
                )
                stats = curs.fetchall()
                if len(stats) == 0:
                    info = {
                        'airport_icao': dep_airport,
                        'date_today': today_date,
                        'airplane_quantity_arrivals': 0,
                        'airplane_quantity_departures': 1
                    }
                    curs.execute(
                        '''
                            INSERT INTO airport_stats (airport_icao, date, airplane_quantity_arrivals, airplane_quantity_departures)
                            VALUES (%(airport_icao)s, %(date_today)s, %(airplane_quantity_arrivals)s, %(airplane_quantity_departures)s)
                        ''', info
                    )
                else:
                    curs.execute(
                        '''
                            UPDATE airport_stats
                            SET airplane_quantity_departures = airplane_quantity_departures + 1
                            WHERE airport_icao = (%s) AND date_today = (%s)
                        ''', (stats[0][1], stats[0][2])
                    )

    def close(self):
        self.conn.close()
