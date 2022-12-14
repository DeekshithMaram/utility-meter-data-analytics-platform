import sys
import boto3, json
from datetime import datetime
from botocore.errorfactory import ClientError
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.dynamicframe import DynamicFrame
from awsglue.transforms import Map
from pyspark.sql.functions import *

s3_resource = boto3.resource('s3')

def check_if_file_exist(bucket, key):
    s3client = boto3.client('s3')
    file_exists = True
    try:
        s3client.head_object(Bucket=bucket, Key=key)
    except ClientError:
        file_exists = False
        pass

    return file_exists


def move_temp_file(bucket, key):
    dt = datetime.now()
    dt.microsecond

    new_file_name = str(dt.microsecond) + '_' + key
    s3_resource.Object(args['temp_workflow_bucket'], new_file_name).copy_from(CopySource="{}/{}".format(bucket, key))
    s3_resource.Object(args['temp_workflow_bucket'], key).delete()


def cleanup_temp_folder(bucket, key):
    if check_if_file_exist(bucket, key):
        move_temp_file(bucket, key)


def is_first_run():
    """
    Checks if the number of job runs for this job is 0.

    TODO: check if at least one successful job is in the

    :return: True if this is the first job run
    """

    client = boto3.client('glue', region_name=args["region"])
    runs = client.get_job_runs(
        JobName=args["JOB_NAME"],
        MaxResults=1
    )

    # return len(runs["JobRuns"]) == 0
    return True # TODO currently only first run is supported


def write_job_state_information(readings):
    """
     get the distinct date value and store them in a temp S3 bucket to now which aggregation data need to be
     calculated later on
    """
    distinct_dates = readings.select('date_str').distinct().collect()
    distinct_dates_str_list = list(value['date_str'] for value in distinct_dates)

    state = {
        "dates": distinct_dates_str_list,
        "first_run": is_first_run()
    }

    s3_resource.Object(args['temp_workflow_bucket'], 'glue_workflow_distinct_dates').put(Body=json.dumps(state))

def schema_contains_field(schema, field_name):
    return len([x for x in schema.fields if x.name == field_name]) > 0

args = getResolvedOptions(sys.argv, ['JOB_NAME', 'db_name', 'table_name', 'clean_data_bucket', 'temp_workflow_bucket', 'region'])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

cleanup_temp_folder(args['temp_workflow_bucket'], 'glue_workflow_distinct_dates')

tableName = args['table_name'].replace("-", "_")
datasource = glueContext.create_dynamic_frame.from_catalog(database = args['db_name'], table_name = tableName, transformation_ctx = "datasource")
schema = datasource.schema()

if (schema.fields[1].name.lower() == 'stdortou'):
    # original london data field index
    field_index = {"id": 0,"datetime": 2, "reading": 3}
else:
    #kaggle ldn data field index
    field_index = {"id": 0,"datetime": 1, "reading": 2}

mapped_readings = ApplyMapping.apply(frame=datasource, mappings=[(schema.fields[field_index["id"]].name, "string", "meter_id", "string"),
                                                                 (schema.fields[field_index["datetime"]].name, "string", "reading_time", "string"),
                                                                 (schema.fields[field_index["reading"]].name, "double", "reading_value", "double")],
                                     transformation_ctx="mapped_readings")

mapped_readings_df = DynamicFrame.toDF(mapped_readings)

if not schema_contains_field(mapped_readings_df.schema, 'obis_code'):
    mapped_readings_df = mapped_readings_df.withColumn("obis_code", lit(""))

if not schema_contains_field(mapped_readings_df.schema, 'reading_type'):
    mapped_readings_df = mapped_readings_df.withColumn("reading_type", lit("INT"))

reading_time = to_timestamp(col("reading_time"), "yyyy-MM-dd HH:mm:ss")
mapped_readings_df = mapped_readings_df \
    .withColumn("week_of_year", weekofyear(reading_time)) \
    .withColumn("date_str", regexp_replace(col("reading_time").substr(1,10), "-", "")) \
    .withColumn("day_of_month", dayofmonth(reading_time)) \
    .withColumn("month", month(reading_time)) \
    .withColumn("year", year(reading_time)) \
    .withColumn("hour", hour(reading_time)) \
    .withColumn("minute", minute(reading_time)) \
    .withColumn("reading_date_time", reading_time) \
    .drop("reading_time")

filteredMeterReads = DynamicFrame.fromDF(mapped_readings_df, glueContext, "filteredMeterReads")

s3_clean_path = "s3://" + args['clean_data_bucket']

glueContext.write_dynamic_frame.from_options(
    frame = filteredMeterReads,
    connection_type = "s3",
    connection_options = {"path": s3_clean_path},
    format = "parquet",
    transformation_ctx = "s3CleanDatasink")

write_job_state_information(mapped_readings_df)

job.commit()

