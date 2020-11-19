#!/usr/bin/env python3
import json
import re
import boto3
from lib import env
from datetime import date, datetime

#This script create a json 



lamdba_client = boto3.client('lambda')
ddb_client = boto3.client('dynamodb')

response = lamdba_client.list_event_source_mappings(
    FunctionName='DynamoToES',
    MaxItems=100
)

def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError ("Type %s not serializable" % type(obj))

table_list = {
	re.search(".+:table\/([a-zA-Z]+)\/.+", event_source["EventSourceArn"]).group(1) : event_source
	for event_source in response["EventSourceMappings"]
}

table_mapping = {
	table_name.lower() : ddb_client.describe_table(TableName=table_name)
	for (table_name, table) in table_list.items()
}
for table_name, table_description in table_mapping.items():
	temp_key_schema = table_description["Table"]["KeySchema"];

	primary_key = "";
	for value in temp_key_schema:
		primary_key = value["AttributeName"] if value["KeyType"] == "HASH" else primary_key

	sort_key = "";
	for value in temp_key_schema:
		sort_key = value["AttributeName"] if value["KeyType"] == "RANGE" else sort_key

	table_description["PrimaryKey"] = primary_key
	if(sort_key != ""):
		table_description["SortKey"] = sort_key

f = open("lib/table_mapping.json", "w")
f.write(json.dumps(table_mapping, default=json_serial,indent=4, sort_keys=True))
f.close()
