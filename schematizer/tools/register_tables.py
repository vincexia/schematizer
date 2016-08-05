# -*- coding: utf-8 -*-
""" This module spins up a Schematizer container and registers all the
mysql tables against the container to test how many tables can be
successfully registered with the Schematizer. It picks up the database
credentials from the topology file and verifies if the returned schema complies
with the input table. Displays the number of successfully registered tables and
failed tables.
"""
from __future__ import absolute_import
from __future__ import unicode_literals

import getpass
import json
import logging
import re
import subprocess
from collections import namedtuple
from contextlib import contextmanager
from optparse import OptionGroup

import pymysql
import requests
from docker import Client
from yelp_batch import Batch
from yelp_batch.batch import batch_command_line_options
from yelp_conn.topology import TopologyFile


logger = logging.getLogger('schematizer.tools.register_tables')

table_info = namedtuple('table_info', 'create_table_stmt columns')


class ContainerUnavailableError(Exception):
    def __init__(self, project='unknown', service='unknown'):
        Exception.__init__(
            self,
            "Container for project {0} and service {1} failed to start".format(
                project, service
            )
        )


class RegisterTables(Batch):

    SERVICE = "schematizerservice"
    HTTP_REQUEST_HEADERS = {
        'content-type': 'application/json',
        'Accept-Charset': 'UTF-8'
    }
    SELECT_QUERY_PATTERN = '^show create table [\w$]+$'
    SHOW_COLUMNS_QUERY_PATTERN = '^show columns from [\w$]+$'
    SHOW_TABLES_QUERY_PATTERN = '^show tables$'

    notify_emails = ['bam+batch@yelp.com']

    @batch_command_line_options
    def parse_options(self, option_parser):
        opt_group = OptionGroup(
            option_parser,
            'RegisterTables Options',
            'This module spins up a Schematizer container and registers all '
            'the mysql tables against the container to test how many tables '
            'can be successfully registered with the Schematizer. Prints the '
            'number of successfully registered tables and failed tables. '
            'NOTE: This batch should be run with at least -v (INFO) verbosity '
            'to view the results.'
        )

        opt_group.add_option(
            '--config_file',
            '-f',
            default='/nail/srv/configs/topology.yaml',
            help='Path of the config file containing db information. '
                 'Default is "%default"'
        )
        opt_group.add_option(
            '--cluster_name',
            '-c',
            default='primary',
            help='Name of the cluster to connect to. Default is "%default"'
        )
        return opt_group

    def get_project(self):
        return "testschematizer{}".format(getpass.getuser())

    def get_container_ip_address(self, project):
        """ Returns container IP address of the first container matching project.
        Raises ContainerUnavailableError if the container is unavailable.

        Args:
            project: Name of the project the container is hosting
        """
        docker_client = Client(version='auto')
        for container in docker_client.containers():
            if container['Labels'].get(
                'com.docker.compose.project'
            ) == project and container['Labels'].get(
                'com.docker.compose.service'
            ) == self.SERVICE:
                return container[
                    'NetworkSettings'
                ]['Networks']['bridge']['IPAddress']
        raise ContainerUnavailableError(project=project, service=self.SERVICE)

    def is_whitelisted(self, query):
        return (
            re.match(self.SELECT_QUERY_PATTERN, query) or
            re.match(self.SHOW_COLUMNS_QUERY_PATTERN, query) or
            re.match(self.SHOW_TABLES_QUERY_PATTERN, query)
        )

    def _execute_query(self, connection, query):
        """ Executes the query and returns the result """
        if self.is_whitelisted(query):
            try:
                with connection.cursor() as cursor:
                    cursor.execute(query)
                    results = cursor.fetchall()
                    return results
            except:
                pass
        return []

    def get_mysql_tables_info(self, conn):
        """ Fetches create table statements and columns of all the tables and
        returns a table_to_info_map with table names as keys and namedtuple of
        create table statement and list of columns as values.
        """
        table_to_info_map = {}
        table_entries = self._execute_query(conn, query='show tables')
        for entry in table_entries:
            table_name = entry[0]
            results = self._execute_query(
                conn,
                query='show create table {}'.format(table_name)
            )
            if results:
                _, create_tbl_stmt = results[0]
                create_tbl_stmt = create_tbl_stmt.replace('\n', '')
                results = self._execute_query(
                    conn,
                    query='show columns from {}'.format(table_name)
                )
                columns = [column[0] for column in results]
                table_to_info_map[table_name] = table_info(
                    create_table_stmt=create_tbl_stmt,
                    columns=columns
                )
        return table_to_info_map

    @contextmanager
    def setup_schematizer_container(self, project):
        """ Set up a scheamtizer container and yields the IP address of the host
        container.
        Note: Removes the container when exiting the context manager.
        """
        try:
            self.run_docker_compose_command(
                '--project-name={}'.format(project),
                'up',
                '-d',
                self.SERVICE
            )
            host = self.get_container_ip_address(project)
            yield host
        finally:
            self.run_docker_compose_command(
                '--project-name={}'.format(project),
                'kill'
            )
            self.run_docker_compose_command(
                '--project-name={}'.format(project),
                'rm',
                '--force'
            )

    def run_docker_compose_command(self, *args):
        """ Executes docker compose command with the given arguments """
        with open("logs/docker-compose.log", "a") as f:
            subprocess.call(
                ['docker-compose'] + list(args),
                stdout=f,
                stderr=subprocess.STDOUT
            )

    def get_connection_param_from_topology(self, topology_file, cluster):
        """ Reads the given topology file and returns the first element in the
        connection params for the given cluster replica ('slave') pair. Throws
        exception if the given cluster, replica pair is not part of this
        toplogy file """
        topology = TopologyFile.new_from_file(topology_file)
        return topology.get_first_connection_param(cluster, 'slave')

    def get_table_to_avro_fields_map(
        self,
        cluster,
        mysql_tables,
        curler,
        host
    ):
        """ Registers the given mysql tables against the Schematizer container.
        """
        payload = {
            'namespace': 'schematizer_test_for_{}'.format(cluster),
            'source_owner_email': 'bam+schematizer_test@yelp.com',
            'contains_pii': False
        }
        table_to_avro_fields_map = {}
        for table_name, table_info in mysql_tables.iteritems():
            payload['source'] = table_name
            payload['new_create_table_stmt'] = table_info.create_table_stmt
            fields = curler(host=host, post_payload=payload)
            table_to_avro_fields_map[table_name] = fields
        return table_to_avro_fields_map

    def post_to_schematizer(self, host, post_payload):
        uri = 'http://{}:8888/v1/schemas/mysql'.format(host)
        response = requests.post(
            uri,
            data=json.dumps(post_payload),
            headers=self.HTTP_REQUEST_HEADERS
        )
        if response.status_code == 200:
            response_json = json.loads(response.json()['schema'])
            fields = response_json['fields']
            return [field['name'] for field in fields]
        return []

    def verify_mysql_table_to_avro_schema(
        self,
        table_to_mysql_columns_map,
        table_to_schema_fields_map
    ):
        """ Compares the mysql_tables columns with the avro schema columns and
        returns a tuple with number of successfully registed tables and
        unregistered tables as values.
        """
        registered_tables = []
        error_tables = []
        for table_name, table_info in table_to_mysql_columns_map.iteritems():
            output_columns = table_to_schema_fields_map.get(table_name)
            if (set(table_info.columns) == set(output_columns) and
                    len(table_info.columns) == len(output_columns)):
                registered_tables.append(table_name)
            else:
                error_tables.append(table_name)

        return registered_tables, error_tables

    @contextmanager
    def setup_connection(self, connection_param):
        """ Connect to a MySQL database with the given connection parameters
        and yields the connection.
        Note: Closes the connection when exiting the context manager.
        """
        connection = None
        try:
            connection = pymysql.connect(
                host=connection_param['host'],
                user=connection_param['user'],
                password=connection_param['passwd'],
                db=connection_param['db'],
                port=connection_param['port'],
                charset=connection_param['charset']
            )
            yield connection
        finally:
            if connection:
                connection.close()

    def run(self):
        conn_param = self.get_connection_param_from_topology(
            self.options.config_file,
            self.options.cluster_name
        )

        with self.setup_connection(conn_param) as conn:
            table_to_info_map = self.get_mysql_tables_info(conn)
        project = self.get_project()
        with self.setup_schematizer_container(project) as host:
            table_to_avro_fields_map = self.get_table_to_avro_fields_map(
                self.options.cluster_name,
                table_to_info_map,
                self.post_to_schematizer,
                host
            )
        registered_tables, error_tables = (
            self.verify_mysql_table_to_avro_schema(
                table_to_info_map,
                table_to_avro_fields_map
            )
        )

        logger.info("Schematizer successfully processed {} tables.".format(
            len(registered_tables)
        ))
        logger.info("Schematizer failed for {} tables which are: {}".format(
            len(error_tables), error_tables
        ))


if __name__ == "__main__":
    RegisterTables().start()