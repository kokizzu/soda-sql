#  Copyright 2020 Soda
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#   http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import logging
from typing import List, Optional

from sodasql.scan.column import Column
from sodasql.scan.custom_metric import CustomMetric
from sodasql.scan.measurement import Measurement
from sodasql.scan.metric import Metric
from sodasql.scan.scan_column_cache import ScanColumnCache
from sodasql.scan.scan_configuration import ScanConfiguration
from sodasql.scan.scan_result import ScanResult
from sodasql.scan.test_result import TestResult
from sodasql.soda_client.soda_client import SodaClient
from sodasql.warehouse.dialect import Dialect
from sodasql.warehouse.warehouse import Warehouse


class Scan:

    def __init__(self,
                 warehouse: Warehouse,
                 scan_configuration: ScanConfiguration = None,
                 custom_metrics: List[CustomMetric] = None,
                 soda_client: SodaClient = None):
        self.soda_client: SodaClient = soda_client
        self.warehouse: Warehouse = warehouse
        self.dialect: Dialect = warehouse.dialect
        self.scan_configuration: ScanConfiguration = scan_configuration
        self.custom_metrics: List[CustomMetric] = custom_metrics

        self.scan_result = ScanResult()

        self.table_sample_clause = \
            f'\nTABLESAMPLE {scan_configuration.sample_method}({scan_configuration.sample_percentage})' \
            if scan_configuration.sample_percentage \
            else None

        self.scan_reference = {
            'warehouse': self.warehouse.name,
            'table_name': self.scan_configuration.table_name,
            'scan_id': 'TODO-generate-an-ID'
        }
        self.columns: Optional[List[Column]] = None
        self.column_caches: Optional[dict] = None

    def execute(self):
        assert self.warehouse.name, 'warehouse.name is required'
        assert self.scan_configuration.table_name, 'scan_configuration.table_name is required'

        self.columns: List[Column] = self.query_columns()

        schema_measurements = [Measurement(Metric.SCHEMA, value=self.columns)]
        self.add_measurements(schema_measurements)

        self.column_caches: dict = \
            {column.name: ScanColumnCache(self.scan_configuration, column, self.dialect) for column in self.columns}

        aggregation_measurements: List[Measurement] = \
            self.query_aggregations()
        self.add_measurements(aggregation_measurements)

        group_by_measurements: List[Measurement] = \
            self.query_group_by_value()
        self.add_measurements(group_by_measurements)

        test_results: List[TestResult] = self.run_tests()
        self.scan_result.test_results.extend(test_results)

        return self.scan_result

    def query_columns(self) -> List[Column]:
        sql = self.warehouse.dialect.sql_columns_metadata_query(self.scan_configuration)
        column_tuples = self.warehouse.execute_query_all(sql)
        columns = []
        for column_tuple in column_tuples:
            name = column_tuple[0]
            type = column_tuple[1]
            nullable = 'YES' == column_tuple[2].upper()
            columns.append(Column(name, type, nullable))
        logging.debug(str(len(columns))+' columns:')
        for column in columns:
            logging.debug(f'  {column.name} {column.type} {"" if column.nullable else "not null"}')
        return columns

    def query_aggregations(self) -> List[Measurement]:
        measurements: List[Measurement] = []

        fields: List[str] = []

        dialect = self.warehouse.dialect
        fields.append(dialect.sql_expr_count_all())
        measurements.append(Measurement(Metric.ROW_COUNT))

        # maps db column names to missing and invalid metric indices in the measurements
        # eg { 'colname': {'missing': 2, 'invalid': 3}, ...}
        column_metric_indices = {}

        for column in self.columns:
            metric_indices = {}
            column_metric_indices[column.name] = metric_indices

            qualified_column_name = dialect.qualify_column_name(column.name)

            column_cache: ScanColumnCache = self.column_caches[column.name]
            column_cache.is_text = dialect.is_text(column)
            column_cache.is_number = dialect.is_number(column)

            is_valid_enabled = \
                (column_cache.validity is not None and column_cache.is_validity_metric_enabled) \
                or self.scan_configuration.is_any_metric_enabled(column.name, [Metric.DISTINCT, Metric.UNIQUENESS])

            is_missing_enabled = is_valid_enabled or column_cache.is_missing_metric_enabled
            non_missing_and_valid_condition = column_cache.non_missing_and_valid_condition
            missing_condition = column_cache.missing_condition
            numeric_text_expr = None

            if is_missing_enabled:
                metric_indices['missing'] = len(measurements)
                fields.append(f'{dialect.sql_expr_count_conditional(missing_condition)}')
                measurements.append(Measurement(Metric.MISSING_COUNT, column.name))

            if is_valid_enabled:
                metric_indices['valid'] = len(measurements)
                fields.append(f'{dialect.sql_expr_count_conditional(non_missing_and_valid_condition)}')
                measurements.append(Measurement(Metric.VALID_COUNT, column.name))

            if column_cache.is_text:
                if self.scan_configuration.is_metric_enabled(column.name, Metric.MIN_LENGTH):
                    length_expr = dialect.sql_expr_conditional(
                        non_missing_and_valid_condition,
                        dialect.sql_expr_length(qualified_column_name))
                    fields.append(dialect.sql_expr_min(length_expr))
                    measurements.append(Measurement(Metric.MIN_LENGTH, column.name))

                if self.scan_configuration.is_metric_enabled(column.name, Metric.MAX_LENGTH):
                    length_expr = dialect.sql_expr_conditional(
                        non_missing_and_valid_condition,
                        dialect.sql_expr_length(qualified_column_name))
                    fields.append(dialect.sql_expr_max(length_expr))
                    measurements.append(Measurement(Metric.MAX_LENGTH, column.name))

                column_cache.validity_format = self.scan_configuration.get_validity_format(column)
                column_cache.is_column_numeric_text_format = \
                    isinstance(column_cache.validity_format, str) \
                    and column_cache.validity_format.startswith('number_')
                if column_cache.is_column_numeric_text_format:
                    numeric_text_expr = dialect.sql_expr_conditional(
                        non_missing_and_valid_condition,
                        dialect.sql_expr_cast_text_to_number(qualified_column_name, column_cache.validity_format))

            if column_cache.is_number or column_cache.is_column_numeric_text_format:
                numeric_expr = qualified_column_name if column_cache.is_number else numeric_text_expr

                if self.scan_configuration.is_metric_enabled(column.name, Metric.MIN):
                    fields.append(dialect.sql_expr_min(numeric_expr))
                    measurements.append(Measurement(Metric.MIN, column.name))

                if self.scan_configuration.is_metric_enabled(column.name, Metric.MAX):
                    fields.append(dialect.sql_expr_max(numeric_expr))
                    measurements.append(Measurement(Metric.MAX, column.name))

                if self.scan_configuration.is_metric_enabled(column.name, Metric.AVG):
                    fields.append(dialect.sql_expr_avg(numeric_expr))
                    measurements.append(Measurement(Metric.AVG, column.name))

                if self.scan_configuration.is_metric_enabled(column.name, Metric.SUM):
                    fields.append(dialect.sql_expr_sum(numeric_expr))
                    measurements.append(Measurement(Metric.SUM, column.name))

        sql = 'SELECT \n  ' + ',\n  '.join(fields) + ' \n' \
              'FROM ' + dialect.qualify_table_name(self.scan_configuration.table_name)
        if self.table_sample_clause:
            sql += f'\n{self.table_sample_clause}'

        query_result_tuple = self.warehouse.execute_query_one(sql)

        for i in range(0, len(measurements)):
            measurement = measurements[i]
            measurement.value = query_result_tuple[i]
            logging.debug(f'Query measurement: {measurement}')

        # Calculating derived measurements
        derived_measurements = []
        row_count = measurements[0].value
        for column in self.columns:
            metric_indices = column_metric_indices[column.name]
            missing_index = metric_indices.get('missing')
            if missing_index is not None:
                missing_count = measurements[missing_index].value
                missing_percentage = missing_count * 100 / row_count
                values_count = row_count - missing_count
                values_percentage = values_count * 100 / row_count
                derived_measurements.append(Measurement(Metric.MISSING_PERCENTAGE, column.name, missing_percentage))
                derived_measurements.append(Measurement(Metric.VALUES_COUNT, column.name, values_count))
                derived_measurements.append(Measurement(Metric.VALUES_PERCENTAGE, column.name, values_percentage))

                valid_index = metric_indices.get('valid')
                if valid_index is not None:
                    valid_count = measurements[valid_index].value
                    invalid_count = row_count - missing_count - valid_count
                    invalid_percentage = invalid_count * 100 / row_count
                    valid_percentage = valid_count * 100 / row_count
                    derived_measurements.append(Measurement(Metric.INVALID_PERCENTAGE, column.name, invalid_percentage))
                    derived_measurements.append(Measurement(Metric.INVALID_COUNT, column.name, invalid_count))
                    derived_measurements.append(Measurement(Metric.VALID_PERCENTAGE, column.name, valid_percentage))

        for derived_measurement in derived_measurements:
            logging.debug(f'Derived measurement: {derived_measurement}')

        measurements.extend(derived_measurements)

        return measurements

    def query_group_by_value(self):
        measurements: List[Measurement] = []

        for column in self.columns:
            # scan_configuration_column: ScanConfigurationColumn = self.scan_configuration.columns.get(column.name)

            group_by_metrics = [Metric.DISTINCT, Metric.UNIQUENESS, Metric.UNIQUE_COUNT,
                                Metric.MINS, Metric.MAXS, Metric.FREQUENT_VALUES]
            if self.scan_configuration.is_any_metric_enabled(column.name, group_by_metrics):

                qualified_column_name = self.dialect.qualify_column_name(column.name)
                qualified_table_name = self.dialect.qualify_table_name(self.scan_configuration.table_name)
                column_cache: ScanColumnCache = self.column_caches[column.name]
                mins_maxs_limit = self.scan_configuration.get_mins_maxs_limit(column.name)
                table_sample_clause = f'\n    AND {self.table_sample_clause} \n' if self.table_sample_clause else ''
                numeric_expr = 'value' \
                    if column_cache.is_number \
                    else self.dialect.sql_expr_cast_text_to_number('value', column_cache.validity_format)

                group_by_cte = (
                    f"WITH group_by_value AS ( \n"
                    f"  SELECT \n"
                    f"    {qualified_column_name} AS value, \n"
                    f"    COUNT(*) AS frequency"
                    f"  FROM {qualified_table_name} \n"
                    f"  WHERE {column_cache.non_missing_and_valid_condition} {table_sample_clause}\n"
                    f"  GROUP BY {qualified_column_name} \n"
                    f")"
                )

                if self.scan_configuration.is_any_metric_enabled(column.name, [
                        Metric.DISTINCT, Metric.UNIQUENESS, Metric.UNIQUE_COUNT]):

                    sql = (f'{group_by_cte} \n'
                           f'SELECT COUNT(*), \n'
                           f'       COUNT(CASE WHEN frequency = 1 THEN 1 END) \n'
                           f'FROM group_by_value')

                    query_result_tuple = self.warehouse.execute_query_one(sql)
                    distinct_count = query_result_tuple[0]
                    measurement = Measurement(Metric.DISTINCT, column.name, distinct_count)
                    measurements.append(measurement)
                    logging.debug(f'Query measurement: {measurement}')

                    unique_count = query_result_tuple[1]
                    measurement = Measurement(Metric.UNIQUE_COUNT, column.name, unique_count)
                    measurements.append(measurement)
                    logging.debug(f'Query measurement: {measurement}')

                    # uniqueness
                    valid_count = self.scan_result.get(Metric.VALID_COUNT, column.name)
                    uniqueness = (distinct_count - 1) * 100 / (valid_count - 1)
                    measurement = Measurement(Metric.UNIQUENESS, column.name, uniqueness)
                    measurements.append(measurement)
                    logging.debug(f'Derived measurement: {measurement}')

                if self.scan_configuration.is_metric_enabled(column.name, Metric.MINS) \
                        and (column_cache.is_number or column_cache.is_column_numeric_text_format):

                    sql = (f'{group_by_cte} \n'
                           f'SELECT value \n'
                           f'FROM group_by_value \n'
                           f'ORDER BY {numeric_expr} ASC \n'
                           f'LIMIT {mins_maxs_limit} \n')

                    rows = self.warehouse.execute_query_all(sql)
                    measurement = Measurement(Metric.MINS, column.name, [row[0] for row in rows])
                    measurements.append(measurement)
                    logging.debug(f'Query measurement: {measurement}')

                if self.scan_configuration.is_metric_enabled(column.name, Metric.MAXS) \
                        and (column_cache.is_number or column_cache.is_column_numeric_text_format):

                    sql = (f'{group_by_cte} \n'
                           f'SELECT value \n'
                           f'FROM group_by_value \n'
                           f'ORDER BY {numeric_expr} DESC \n'
                           f'LIMIT {mins_maxs_limit} \n')

                    rows = self.warehouse.execute_query_all(sql)
                    measurement = Measurement(Metric.MAXS, column.name, [row[0] for row in rows])
                    measurements.append(measurement)
                    logging.debug(f'Query measurement: {measurement}')

                if self.scan_configuration.is_metric_enabled(column.name, Metric.FREQUENT_VALUES) \
                        and (column_cache.is_number or column_cache.is_column_numeric_text_format):

                    frequent_values_limit = self.scan_configuration.get_frequent_values_limit(column.name)
                    sql = (f'{group_by_cte} \n'
                           f'SELECT value \n'
                           f'FROM group_by_value \n'
                           f'ORDER BY frequency DESC \n'
                           f'LIMIT {frequent_values_limit} \n')

                    rows = self.warehouse.execute_query_all(sql)
                    measurement = Measurement(Metric.FREQUENT_VALUES, column.name, [row[0] for row in rows])
                    measurements.append(measurement)
                    logging.debug(f'Query measurement: {measurement}')

        return measurements

    def run_tests(self):
        test_results = []
        if self.scan_configuration and self.scan_configuration.columns:
            for column_name in self.scan_configuration.columns:
                scan_configuration_column = self.scan_configuration.columns.get(column_name)
                if scan_configuration_column.tests:
                    column_measurement_values = {
                        measurement.metric: measurement.value
                        for measurement in self.scan_result.measurements
                        if measurement.column_name == column_name
                    }
                    for test in scan_configuration_column.tests:
                        test_values = {metric: value for metric, value in column_measurement_values.items() if metric in test}
                        test_outcome = True if eval(test, test_values) else False
                        test_results.append(TestResult(test_outcome, test, test_values, column_name))
        return test_results

    def add_measurements(self, measurements):
        if measurements:
            self.scan_result.measurements.extend(measurements)
            if self.soda_client:
                self.soda_client.send_measurements(self.scan_reference, measurements)
