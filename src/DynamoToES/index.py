import json
import re
import boto3
from lib import env
from elasticsearch import Elasticsearch, RequestsHttpConnection
from opensearchpy import OpenSearch
from requests_aws4auth import AWS4Auth
import json
import os.path
import importlib.util
import traceback

reserved_fields = [
    "uid",
    "_id",
    "_type",
    "_source",
    "_all",
    "_parent",
    "_fieldnames",
    "_routing",
    "_index",
    "_size",
    "_timestamp",
    "_ttl",
]


# Process DynamoDB Stream records and insert the object in ElasticSearch
# Use the Table name as index and doc_type name
# Force index refresh upon all actions for close to realtime reindexing
# Use IAM Role for authentication
# Properly unmarshal DynamoDB JSON types. Binary NOT tested.


# Load the mapping if it exist
table_mapping = None
if os.path.isfile("lib/table_mapping.json"):
    with open("lib/table_mapping.json") as json_file:
        table_mapping = json.load(json_file)


def lambda_handler(event, context):

    session = boto3.session.Session()
    credentials = session.get_credentials()

    # Get proper credentials for ES auth
    awsauth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        session.region_name,
        "es",
        session_token=credentials.token,
    )

    # Connect to ES
    es_client = None
    if env.ES_ENDPOINT is not None:
        es_client = Elasticsearch(
            [env.ES_ENDPOINT],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
        )

    os_client = None
    if env.OS_ENDPOINT is not None:
        os_client = OpenSearch(
            [env.OS_ENDPOINT],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
        )

    if es_client:
        print("Cluster Elasticsearch info:")
        print(es_client.info())

    if os_client:
        print("Cluster Opensearch info:")
        print(os_client.info())

    # Loop over the DynamoDB Stream records
    for record in event["Records"]:

        try:
            if record["eventName"] == "INSERT":
                if es_client:
                    insert_document(es_client, record)
                if os_client:
                    insert_document(os_client, record)
            elif record["eventName"] == "REMOVE":
                if es_client:
                    remove_document(es_client, record)
                if os_client:
                    remove_document(os_client, record)
            elif record["eventName"] == "MODIFY":
                if es_client:
                    modify_document(es_client, record)
                if os_client:
                    modify_document(os_client, record)

        except Exception as e:
            print("Failed to process:")
            print(json.dumps(record))
            print("ERROR: " + repr(e))
            print(traceback.format_exc())
            continue


# Process MODIFY events
def modify_document(search_client, record):
    table = getTable(record)
    print("Dynamo Table: " + table)

    docId = generateId(record, table)
    print("KEY")
    print(docId)

    # Unmarshal the DynamoDB JSON to a normal JSON
    doc = json.dumps(unmarshalJson(record["dynamodb"]["NewImage"]))

    print("Updated document:")
    print(doc)

    # We reindex the whole document as ES accepts partial docs
    if type(search_client) is Elasticsearch:
        search_client.index(
            index=table, body=doc, id=docId, doc_type=table, refresh=True
        )
    else:
        search_client.index(
            index=table,
            body=doc,
            id=docId,
            refresh=True,
            headers={"Content-Type": "application/json"},
        )

    print("Successly modified - Index: " + table + " - Document ID: " + docId)


# Process REMOVE events
def remove_document(search_client, record):
    table = getTable(record)
    print("Dynamo Table: " + table)

    docId = generateId(record, table)
    print("Deleting document ID: " + docId)
    if type(search_client) is Elasticsearch:
        search_client.delete(index=table, id=docId, doc_type=table, refresh=True)
    else:
        search_client.delete(
            index=table,
            id=docId,
            refresh=True,
            headers={"Content-Type": "application/json"},
        )

    print("Successly removed - Index: " + table + " - Document ID: " + docId)


# Process INSERT events
def insert_document(search_client, record):
    table = getTable(record)
    print("Dynamo Table: " + table)

    # Unmarshal the DynamoDB JSON to a normal JSON
    doc = json.dumps(unmarshalJson(record["dynamodb"]["NewImage"]))

    print("New document to Index:")
    print(doc)

    newId = generateId(record, table)
    if type(search_client) is Elasticsearch:
        # Create index if missing
        if search_client.indices.exists(table) == False:
            print("Create missing index: " + table)

            search_client.indices.create(
                table, body='{"settings": { "index.mapping.coerce": true } }'
            )

            print("Index created: " + table)

        search_client.index(
            index=table, body=doc, id=newId, doc_type=table, refresh=True
        )
    else:
        search_client.index(
            index=table,
            body=doc,
            id=newId,
            refresh=True,
            headers={"Content-Type": "application/json"},
        )

    print("Successly inserted - Index: " + table + " - Document ID: " + newId)


# Return the dynamoDB table that received the event. Lower case it
def getTable(record):
    p = re.compile("arn:aws:dynamodb:.*?:.*?:table/([0-9a-zA-Z_-]+)/.+")
    m = p.match(record["eventSourceARN"])
    if m is None:
        raise Exception("Table not found in SourceARN")
    return m.group(1).lower()


# Generate the ID for ES. Used for deleting or updating item later
# By default using keys given by the dynamo stream
# If a mapping is there, it's used to create the id
def generateId(record, table_name):
    keys = unmarshalJson(record["dynamodb"]["Keys"])
    if table_mapping != None and table_name in table_mapping.keys():
        print("Use mapping")
        if "SortKey" in table_mapping[table_name]:
            return (
                str(keys[table_mapping[table_name]["PrimaryKey"]])
                + "|"
                + str(keys[table_mapping[table_name]["SortKey"]])
            )
        else:
            return str(keys[table_mapping[table_name]["PrimaryKey"]])

    # Concat HASH and RANGE key with | in between
    newId = ""
    i = 0
    for key, value in list(keys.items()):
        if i > 0:
            newId += "|"
        newId += str(value)
        i += 1

    return newId


# Unmarshal a JSON that is DynamoDB formatted
def unmarshalJson(node):
    data = {}
    data["M"] = node
    return unmarshalValue(data, True)


# ForceNum will force float or Integer to
def unmarshalValue(node, forceNum=False):
    for key, value in list(node.items()):
        if key == "NULL":
            return None
        if key == "S" or key == "BOOL":
            return value
        if key == "N":
            if forceNum:
                return int_or_float(value)
            return value
        if key == "M":
            data = {}
            for key1, value1 in list(value.items()):
                if key1 in reserved_fields:
                    key1 = key1.replace("_", "__", 1)
                data[key1] = unmarshalValue(value1, True)
            return data
        if key == "BS" or key == "L":
            data = []
            for item in value:
                data.append(unmarshalValue(item))
            return data
        if key == "SS":
            data = []
            for item in value:
                data.append(item)
            return data
        if key == "NS":
            data = []
            for item in value:
                if forceNum:
                    data.append(int_or_float(item))
                else:
                    data.append(item)
            return data


# Detect number type and return the correct one
def int_or_float(s):
    try:
        return int(s)
    except ValueError:
        return float(s)
