from copy import deepcopy
from enum import Enum
import itertools
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import TypedDict
from typing import Union
from typing import cast

from ddtrace._trace._span_pointer import _SpanPointerDescription
from ddtrace._trace._span_pointer import _SpanPointerDirection
from ddtrace._trace._span_pointer import _standard_hashing_function
from ddtrace._trace.utils_botocore.span_pointers.telemetry import record_span_pointer_calculation_issue
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


def _boto3_dynamodb_types_TypeSerializer_serialize(value):
    # We need this serializer for some of the code below, but we don't want to
    # import boto3 things at the top level of this module since not everyone
    # who is using ddtrace also needs boto3. Any code that actually does reach
    # the serialization functionality below *will* have boto3 available.
    from boto3.dynamodb.types import TypeSerializer

    return TypeSerializer().serialize(value)


class _TelemetryIssueTags(Enum):
    REQUEST_PARAMETERS = "request_parameters"
    HASHING_FAILURE = "hashing_failure"
    MISSING_TABLE_INFO = "missing_table_info"
    PROCESSED_ITEMS_CALCULATION = "processed_items_calculation"
    PRIMARY_KEY_ISSUE = "primary_key_issue"


_DynamoDBTableName = str
_DynamoDBItemFieldName = str
_DynamoDBItemTypeTag = str

# _DynamoDBItemValueObject is shaped like {"S": "something"}, the form that the
# lower level DynamoDB API expects.
_DynamoDBItemValueObject = Dict[_DynamoDBItemTypeTag, Any]
# _DynamoDBItemValueDeserialized is the python-native form of the value. The
# resource-based boto3 APIs for DynamoDB accept this form and handle the
# serialization into something like the _DyanmoDBItemValueObject using the
# TypeSerializer.
_DynamoDBItemValueDeserialized = Any
_DynamoDBItemValue = Union[_DynamoDBItemValueObject, _DynamoDBItemValueDeserialized]
_DynamoDBItem = Dict[_DynamoDBItemFieldName, _DynamoDBItemValue]

_DynamoDBItemPrimaryKeyValue = Dict[_DynamoDBItemTypeTag, str]  # must be length 1
_DynamoDBItemPrimaryKey = Dict[_DynamoDBItemFieldName, _DynamoDBItemPrimaryKeyValue]


class _DynamoDBPutRequest(TypedDict):
    Item: _DynamoDBItem


class _DynamoDBPutRequestWriteRequest(TypedDict):
    PutRequest: _DynamoDBPutRequest


class _DynamoDBDeleteRequest(TypedDict):
    Key: _DynamoDBItemPrimaryKey


class _DynamoDBDeleteRequestWriteRequest(TypedDict):
    DeleteRequest: _DynamoDBDeleteRequest


_DynamoDBWriteRequest = Union[_DynamoDBPutRequestWriteRequest, _DynamoDBDeleteRequestWriteRequest]


class _DynamoDBTransactConditionCheck(TypedDict, total=False):
    # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_ConditionCheck.html
    Key: _DynamoDBItemPrimaryKey
    TableName: _DynamoDBTableName


class _DynamoDBTransactConditionCheckItem(TypedDict):
    ConditionCheck: _DynamoDBTransactConditionCheck


class _DynanmoDBTransactDelete(TypedDict, total=False):
    # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Delete.html
    Key: _DynamoDBItemPrimaryKey
    TableName: _DynamoDBTableName


class _DynamoDBTransactDeleteItem(TypedDict):
    Delete: _DynanmoDBTransactDelete


class _DynamoDBTransactPut(TypedDict, total=False):
    # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Put.html
    Item: _DynamoDBItem
    TableName: _DynamoDBTableName


class _DynamoDBTransactPutItem(TypedDict):
    Put: _DynamoDBTransactPut


class _DynamoDBTransactUpdate(TypedDict, total=False):
    # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Update.html
    Key: _DynamoDBItemPrimaryKey
    TableName: _DynamoDBTableName


class _DynamoDBTransactUpdateItem(TypedDict):
    Update: _DynamoDBTransactUpdate


# https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItem.html
_DynamoDBTransactWriteItem = Union[
    _DynamoDBTransactConditionCheckItem,
    _DynamoDBTransactDeleteItem,
    _DynamoDBTransactPutItem,
    _DynamoDBTransactUpdateItem,
]


_OPERATION_BASE = "DynamoDB."


def _extract_span_pointers_for_dynamodb_response(
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    operation_name: str,
    request_parameters: Dict[str, Any],
    response: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    if operation_name == "PutItem":
        return _extract_span_pointers_for_dynamodb_putitem_response(
            dynamodb_primary_key_names_for_tables, request_parameters
        )

    elif operation_name in ("UpdateItem", "DeleteItem"):
        return _extract_span_pointers_for_dynamodb_keyed_operation_response(
            operation_name,
            request_parameters,
        )

    elif operation_name == "BatchWriteItem":
        return _extract_span_pointers_for_dynamodb_batchwriteitem_response(
            dynamodb_primary_key_names_for_tables,
            request_parameters,
            response,
        )

    elif operation_name == "TransactWriteItems":
        return _extract_span_pointers_for_dynamodb_transactwriteitems_response(
            dynamodb_primary_key_names_for_tables,
            request_parameters,
        )

    else:
        return []


def _extract_span_pointers_for_dynamodb_putitem_response(
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    request_parameters: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    operation = _OPERATION_BASE + "PutItem"

    try:
        table_name = request_parameters["TableName"]
        item = request_parameters["Item"]
    except KeyError as e:
        log.debug(
            "span pointers: failed to extract %s span pointer: missing key %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return []

    primary_key_names = _extract_primary_key_names_from_configuration(
        operation=operation,
        dynamodb_primary_key_names_for_tables=dynamodb_primary_key_names_for_tables,
        table_name=table_name,
    )
    if primary_key_names is None:
        return []

    primary_key = _aws_dynamodb_item_primary_key_from_item(
        operation=operation,
        primary_key_field_names=primary_key_names,
        item=item,
    )
    if primary_key is None:
        return []

    try:
        span_pointer_description = _aws_dynamodb_item_span_pointer_description(
            operation=operation,
            pointer_direction=_SpanPointerDirection.DOWNSTREAM,
            table_name=table_name,
            primary_key=primary_key,
        )
        if span_pointer_description is None:
            return []

        return [span_pointer_description]

    except Exception as e:
        log.debug(
            "span pointers: failed to generate %s span pointer: %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag=_TelemetryIssueTags.HASHING_FAILURE.value)
        return []


def _extract_primary_key_names_from_configuration(
    operation: str,
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    table_name: _DynamoDBTableName,
) -> Optional[Set[_DynamoDBItemFieldName]]:
    try:
        return dynamodb_primary_key_names_for_tables[table_name]
    except KeyError as e:
        log.debug(
            "span pointers: failed to extract %s span pointer: table %s not found in primary key names; "
            "Please set them through ddtrace.config.botocore['dynamodb_primary_key_names_for_tables'] or "
            "DD_BOTOCORE_DYNAMODB_TABLE_PRIMARY_KEYS",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.MISSING_TABLE_INFO.value
        )
        return None


def _extract_span_pointers_for_dynamodb_keyed_operation_response(
    operation_name: str,
    request_parmeters: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    operation = _OPERATION_BASE + operation_name

    try:
        table_name = request_parmeters["TableName"]
        key = request_parmeters["Key"]
    except KeyError as e:
        log.debug(
            "span pointers: failed to extract %s span pointer: missing key %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return []

    try:
        span_pointer_description = _aws_dynamodb_item_span_pointer_description(
            operation=operation,
            pointer_direction=_SpanPointerDirection.DOWNSTREAM,
            table_name=table_name,
            primary_key=key,
        )
        if span_pointer_description is None:
            return []

        return [span_pointer_description]

    except Exception as e:
        log.debug(
            "span pointers: failed to generate %s span pointer: %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag=_TelemetryIssueTags.HASHING_FAILURE.value)
        return []


def _extract_span_pointers_for_dynamodb_batchwriteitem_response(
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    request_parameters: Dict[str, Any],
    response: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    operation = _OPERATION_BASE + "BatchWriteItem"

    try:
        requested_items = request_parameters["RequestItems"]
        unprocessed_items = response.get("UnprocessedItems", {})

        processed_items = _identify_dynamodb_batch_write_item_processed_items(requested_items, unprocessed_items)
        if processed_items is None:
            return []

    except Exception as e:
        log.debug(
            "span pointers: failed to extract %s span pointers: %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return []

    try:
        result = []
        for table_name, processed_items_for_table in processed_items.items():
            for write_request in processed_items_for_table:
                primary_key = _aws_dynamodb_item_primary_key_from_write_request(
                    dynamodb_primary_key_names_for_tables=dynamodb_primary_key_names_for_tables,
                    table_name=table_name,
                    write_request=write_request,
                )
                if primary_key is None:
                    return []

                span_pointer_description = _aws_dynamodb_item_span_pointer_description(
                    operation=operation,
                    pointer_direction=_SpanPointerDirection.DOWNSTREAM,
                    table_name=table_name,
                    primary_key=primary_key,
                )
                if span_pointer_description is None:
                    return []

                result.append(span_pointer_description)

        return result

    except Exception as e:
        log.debug(
            "span pointers: failed to generate %s span pointer: %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag=_TelemetryIssueTags.HASHING_FAILURE.value)
        return []


def _extract_span_pointers_for_dynamodb_transactwriteitems_response(
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    request_parameters: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    operation = _OPERATION_BASE + "TransactWriteItems"
    try:
        return list(
            itertools.chain.from_iterable(
                _aws_dynamodb_item_span_pointer_description_for_transactwrite_request(
                    dynamodb_primary_key_names_for_tables=dynamodb_primary_key_names_for_tables,
                    transact_write_request=transact_write_request,
                )
                for transact_write_request in request_parameters["TransactItems"]
            )
        )

    except Exception as e:
        log.debug(
            "span pointers: failed to generate %s span pointer: %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag=_TelemetryIssueTags.HASHING_FAILURE.value)
        return []


def _identify_dynamodb_batch_write_item_processed_items(
    requested_items: Dict[_DynamoDBTableName, List[_DynamoDBWriteRequest]],
    unprocessed_items: Dict[_DynamoDBTableName, List[_DynamoDBWriteRequest]],
) -> Optional[Dict[_DynamoDBTableName, List[_DynamoDBWriteRequest]]]:
    operation = _OPERATION_BASE + "BatchWriteItem"

    processed_items = {}

    if not all(table_name in requested_items for table_name in unprocessed_items):
        log.debug("span pointers: %s unprocessed items include tables not in the requested items", operation)
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PROCESSED_ITEMS_CALCULATION.value
        )
        return None

    for table_name, requested_write_requests in requested_items.items():
        if table_name not in unprocessed_items:
            processed_items[table_name] = deepcopy(requested_write_requests)

        else:
            if not all(
                unprocessed_write_request in requested_write_requests
                for unprocessed_write_request in unprocessed_items[table_name]
            ):
                log.debug(
                    "span pointers: %s unprocessed write requests include items not in the requested write requests",
                    operation,
                )
                record_span_pointer_calculation_issue(
                    operation=operation, issue_tag=_TelemetryIssueTags.PROCESSED_ITEMS_CALCULATION.value
                )
                return None

            these_processed_items = [
                deepcopy(processed_write_request)
                for processed_write_request in requested_write_requests
                if processed_write_request not in unprocessed_items[table_name]
            ]
            if these_processed_items:
                # no need to include them if they are all unprocessed
                processed_items[table_name] = these_processed_items

    return processed_items


def _aws_dynamodb_item_primary_key_from_item(
    operation: str,
    primary_key_field_names: Set[_DynamoDBItemFieldName],
    item: _DynamoDBItem,
) -> Optional[_DynamoDBItemPrimaryKey]:
    if len(primary_key_field_names) not in (1, 2):
        log.debug("span pointers: unexpected number of primary key fields: %d", len(primary_key_field_names))
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None

    result = {}
    for primary_key_field_name in primary_key_field_names:
        primary_key_field_value = _aws_dynamodb_extract_and_verify_primary_key_field_value_item(
            operation, item, primary_key_field_name
        )
        if primary_key_field_value is None:
            return None

        result[primary_key_field_name] = primary_key_field_value

    return result


def _aws_dynamodb_item_primary_key_from_write_request(
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    table_name: _DynamoDBTableName,
    write_request: _DynamoDBWriteRequest,
) -> Optional[_DynamoDBItemPrimaryKey]:
    # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_WriteRequest.html

    operation = _OPERATION_BASE + "BatchWriteItem"

    if len(write_request) != 1:
        log.debug("span pointers: unexpected number of write request fields: %d", len(write_request))
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return None

    if "PutRequest" in write_request:
        # Unfortunately mypy doesn't properly see the if statement above as a
        # type-narrowing from _DynamoDBWriteRequest to
        # _DynamoDBPutRequestWriteRequest, so we help it out ourselves.
        write_request = cast(_DynamoDBPutRequestWriteRequest, write_request)

        primary_key_field_names = _extract_primary_key_names_from_configuration(
            operation=operation,
            dynamodb_primary_key_names_for_tables=dynamodb_primary_key_names_for_tables,
            table_name=table_name,
        )
        if primary_key_field_names is None:
            return None

        return _aws_dynamodb_item_primary_key_from_item(
            operation=operation,
            primary_key_field_names=primary_key_field_names,
            item=write_request["PutRequest"]["Item"],
        )

    elif "DeleteRequest" in write_request:
        # Unfortunately mypy doesn't properly see the if statement above as a
        # type-narrowing from _DynamoDBWriteRequest to
        # _DynamoDBDeleteRequestWriteRequest, so we help it out ourselves.
        write_request = cast(_DynamoDBDeleteRequestWriteRequest, write_request)

        return write_request["DeleteRequest"]["Key"]

    else:
        log.debug("span pointers: unexpected write request structure: %s", "".join(sorted(write_request.keys())))
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return None


def _aws_dynamodb_item_span_pointer_description_for_transactwrite_request(
    dynamodb_primary_key_names_for_tables: Dict[_DynamoDBTableName, Set[_DynamoDBItemFieldName]],
    transact_write_request: _DynamoDBTransactWriteItem,
) -> List[_SpanPointerDescription]:
    operation = _OPERATION_BASE + "TransactWriteItems"

    if len(transact_write_request) != 1:
        log.debug("span pointers: unexpected number of transact write request fields: %d", len(transact_write_request))
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return []

    if "ConditionCheck" in transact_write_request:
        # ConditionCheck requests don't actually modify anything, so we don't
        # consider the associated item to be passing information between spans.
        return []

    elif "Delete" in transact_write_request:
        # Unfortunately mypy does not properly see the if statement above as a
        # type-narrowing from _DynamoDBTransactWriteItem to
        # _DynamoDBTransactDeleteItem, so we help it out ourselves.

        transact_write_request = cast(_DynamoDBTransactDeleteItem, transact_write_request)

        table_name = transact_write_request["Delete"]["TableName"]
        key = transact_write_request["Delete"]["Key"]

    elif "Put" in transact_write_request:
        # Unfortunately mypy does not properly see the if statement above as a
        # type-narrowing from _DynamoDBTransactWriteItem to
        # _DynamoDBTransactPutItem, so we help it out ourselves.

        transact_write_request = cast(_DynamoDBTransactPutItem, transact_write_request)

        table_name = transact_write_request["Put"]["TableName"]

        primary_key_field_names = _extract_primary_key_names_from_configuration(
            operation=operation,
            dynamodb_primary_key_names_for_tables=dynamodb_primary_key_names_for_tables,
            table_name=table_name,
        )
        if primary_key_field_names is None:
            return []

        primary_key = _aws_dynamodb_item_primary_key_from_item(
            operation=operation,
            primary_key_field_names=primary_key_field_names,
            item=transact_write_request["Put"]["Item"],
        )
        if primary_key is None:
            return []
        key = primary_key

    elif "Update" in transact_write_request:
        # Unfortunately mypy does not properly see the if statement above as a
        # type-narrowing from _DynamoDBTransactWriteItem to
        # _DynamoDBTransactUpdateItem, so we help it out ourselves.

        transact_write_request = cast(_DynamoDBTransactUpdateItem, transact_write_request)

        table_name = transact_write_request["Update"]["TableName"]
        key = transact_write_request["Update"]["Key"]

    else:
        log.debug(
            "span pointers: unexpected transact write request structure: %s",
            "".join(sorted(transact_write_request.keys())),
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.REQUEST_PARAMETERS.value
        )
        return []

    span_pointer_description = _aws_dynamodb_item_span_pointer_description(
        operation=operation,
        pointer_direction=_SpanPointerDirection.DOWNSTREAM,
        table_name=table_name,
        primary_key=key,
    )
    if span_pointer_description is None:
        return []

    return [span_pointer_description]


def _aws_dynamodb_item_span_pointer_description(
    operation: str,
    pointer_direction: _SpanPointerDirection,
    table_name: _DynamoDBTableName,
    primary_key: _DynamoDBItemPrimaryKey,
) -> Optional[_SpanPointerDescription]:
    pointer_hash = _aws_dynamodb_item_span_pointer_hash(operation, table_name, primary_key)
    if pointer_hash is None:
        return None

    return _SpanPointerDescription(
        pointer_kind="aws.dynamodb.item",
        pointer_direction=pointer_direction,
        pointer_hash=pointer_hash,
        extra_attributes={},
    )


def _aws_dynamodb_extract_and_verify_primary_key_field_value_item(
    operation: str,
    item: _DynamoDBItem,
    primary_key_field_name: _DynamoDBItemFieldName,
) -> Optional[_DynamoDBItemPrimaryKeyValue]:
    if primary_key_field_name not in item:
        log.debug("span pointers: missing primary key field: %s", primary_key_field_name)
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None

    value_object = _aws_dynamodb_item_value_to_probably_primary_key_value(
        operation=operation,
        item_value=item[primary_key_field_name],
    )
    if value_object is None:
        return None

    if len(value_object) != 1:
        log.debug(
            "span pointers: primary key field %s must have exactly one value: %d",
            primary_key_field_name,
            len(value_object),
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None

    value_type, value_data = next(iter(value_object.items()))
    if value_type not in ("S", "N", "B"):
        log.debug("span pointers: unexpected primary key field %s value type: %s", primary_key_field_name, value_type)
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None

    if not isinstance(value_data, str):
        log.debug(
            "span pointers: unexpected primary key field %s value data type: %s",
            primary_key_field_name,
            type(value_data),
        )
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None

    return {value_type: value_data}


def _aws_dynamodb_item_value_to_probably_primary_key_value(
    operation: str, item_value: _DynamoDBItemValue
) -> Optional[_DynamoDBItemPrimaryKeyValue]:
    # If the item_value looks more or less like a primary key, we return it and
    # let the caller decide what to do with it. Otherwise we use the type
    # serializer and hope that does the right thing.

    if (
        isinstance(item_value, dict)
        and len(item_value) == 1
        and all(isinstance(part, str) for part in itertools.chain.from_iterable(item_value.items()))
    ):
        return item_value

    try:
        return cast(_DynamoDBItemPrimaryKeyValue, _boto3_dynamodb_types_TypeSerializer_serialize(item_value))

    except Exception as e:
        log.debug("span pointers: failed to serialize item value to botocore value: %s", e)
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None


def _aws_dynamodb_item_span_pointer_hash(
    operation: str, table_name: _DynamoDBTableName, primary_key: _DynamoDBItemPrimaryKey
) -> Optional[str]:
    try:
        if len(primary_key) == 1:
            key, value_object = next(iter(primary_key.items()))
            encoded_key_1 = key.encode("utf-8")

            encoded_value_1 = _aws_dynamodb_item_encode_primary_key_value(operation, value_object)
            if encoded_value_1 is None:
                return None

            encoded_key_2 = b""
            encoded_value_2 = b""

        elif len(primary_key) == 2:
            (key_1, value_object_1), (key_2, value_object_2) = sorted(
                primary_key.items(), key=lambda x: x[0].encode("utf-8")
            )
            encoded_key_1 = key_1.encode("utf-8")

            encoded_value_1 = _aws_dynamodb_item_encode_primary_key_value(operation, value_object_1)
            if encoded_value_1 is None:
                return None

            encoded_key_2 = key_2.encode("utf-8")

            maybe_encoded_value_2 = _aws_dynamodb_item_encode_primary_key_value(operation, value_object_2)
            if maybe_encoded_value_2 is None:
                return None
            encoded_value_2 = maybe_encoded_value_2

        else:
            log.debug("span pointers: unexpected number of primary key fields: %d", len(primary_key))
            record_span_pointer_calculation_issue(
                operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
            )
            return None

        return _standard_hashing_function(
            table_name.encode("utf-8"),
            encoded_key_1,
            encoded_value_1,
            encoded_key_2,
            encoded_value_2,
        )

    except Exception as e:
        log.debug("span pointers: failed to generate %s span pointer hash: %s", operation, e)
        record_span_pointer_calculation_issue(operation=operation, issue_tag=_TelemetryIssueTags.HASHING_FAILURE.value)
        return None


def _aws_dynamodb_item_encode_primary_key_value(
    operation: str, value_object: _DynamoDBItemPrimaryKeyValue
) -> Optional[bytes]:
    try:
        if not isinstance(value_object, dict):
            try:
                value_object = _boto3_dynamodb_types_TypeSerializer_serialize(value_object)
            except Exception as e:
                log.debug("span pointers: failed to serialize primary key value to botocore value: %s", e)
                record_span_pointer_calculation_issue(
                    operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
                )
                return None

        if len(value_object) != 1:
            log.debug("span pointers: primary key value object must have exactly one field: %d", len(value_object))
            record_span_pointer_calculation_issue(
                operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
            )
            return None

        value_type, value = next(iter(value_object.items()))

        if value_type == "S":
            return value.encode("utf-8")

        if value_type in ("N", "B"):
            # these should already be here as ASCII strings, though B is
            # sometimes already bytes.
            if isinstance(value, bytes):
                return value
            return value.encode("ascii")

        log.debug("span pointers: unexpected primary key value type: %s", value_type)
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None

    except Exception as e:
        log.debug("span pointers: failed to encode primary key value for %s: %s", operation, e)
        record_span_pointer_calculation_issue(
            operation=operation, issue_tag=_TelemetryIssueTags.PRIMARY_KEY_ISSUE.value
        )
        return None
