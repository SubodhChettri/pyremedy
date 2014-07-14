from __future__ import print_function

from ctypes import (
    CDLL, sizeof, cast, byref, memset, c_char_p, c_uint, POINTER
)
from datetime import datetime
import platform

from . import arh
from .exceptions import ARSError


class ARS(object):
    """The ARS object implements a simple CRUD interface for Remedy ARS
    servers.  It is passed server details and credentials and acts as the main
    object which talks to Remedy ARS.

    The ARS object also provides a caching mechanism for schema lists and
    field mappings.

    :param server: the Remedy ARS server to connect to
    :param user: the username to authenticate with
    :param password: the password to authenticate with
    :param port: the port number of the server
    :param rpc_program_number: the RPC program number of the server
    """

    def __init__(self, server, user, password, port=0, rpc_program_number=0):
        # Determine the architecture of the user's system
        bits, linkage = platform.architecture()

        #: The Remedy ARS C API shared object file which is used to interact
        #: with the Remedy server
        if bits == '64bit':
            self.arlib = CDLL('libar_lx64.so')
        else:
            self.arlib = CDLL('libar.so')

        #: The standard C library used to run several lower-lever C functions
        self.clib = CDLL('libc.so.6')

        #: The control record for each operation containing details about the
        #: user and session performing each operation
        self.control = arh.ARControlStruct()

        #: A list of warnings or errors generated from each call to Remedy ARS
        self.status = arh.ARStatusList()

        #: A list of tuples containing errors that occurred on the last call
        self.errors = []

        #: A simple cache containing all schemas
        self.schema_cache = None

        #: A simple cache containing field id to name mappings for schemas
        self.field_name_cache = {}

        #: A simple cache containing field name to id mappings for schemas
        self.field_name_cache_rev = {}

        #: A simple cache containing field enum mappings for a particular field
        self.field_enum_cache = {}

        # Initialise control to 0 for safety
        memset(byref(self.control), 0, sizeof(arh.ARControlStruct))

        # Load the ARControlStruct with server details and user credentials
        self.control.server = server
        self.control.user = user
        self.control.password = password

        # Note on FreeAR functions:
        #
        # FreeAR functions are used to clear the contents of memory for
        # particular struct types.  These functions are used when a Remedy ARS
        # operation fills a struct as a return value.
        #
        # The second argument in the FreeAR functions is a boolean
        # known as freeStruct which specifies whether the memory should be
        # deallocated along with the contents.

        # Performs server initalisation
        if (
            self.arlib.ARInitialization(
                # ARControlStruct *control: the control record
                byref(self.control),

                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError(
                'Enable to perform initialisation against server '
                '{}'.format(server)
            )

        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

        # Set the server port and/or RPC program number (if specified)
        if port or rpc_program_number:
            if (
                self.arlib.ARSetServerPort(
                    # ARControlStruct *control: the control record
                    byref(self.control),
                    # ARNameType server: the server to update with the port
                    self.control.server,
                    # int port: the port number
                    port,
                    # int rpcProgramNum: the RPC program of the server
                    rpc_program_number,

                    # (return) ARStatusList *status: notes, warnings or errors
                    # generated by the operation
                    byref(self.status)
                ) >= arh.AR_RETURN_ERROR
            ):
                self._update_errors()
                self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
                raise ARSError(
                    'Unable to set the port to {} and RPC program number to '
                    '{} for server {}'.format(port, rpc_program_number, server)
                )

            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

    def terminate(self):
        """Perform a cleanup and disconnect the session"""
        if (
            self.arlib.ARTermination(
                # ARControlStruct *control: the control record
                byref(self.control),

                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError('Enable to terminate the server connection')

        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

    def schemas(self):
        """Retrieves a list of all available schemas on the specified Remedy
        ARS server
        """

        # Use the cache if possible
        if self.schema_cache is not None:
            return self.schema_cache

        schema_list = arh.ARNameList()

        if (
            self.arlib.ARGetListSchema(
                # ARControlStruct *control: the control record
                byref(self.control),
                # ARTimestamp changedSince: retrieves forms with a chosen
                # modification timestamp
                0,
                # unsigned int schemaType: get all schemas
                arh.AR_LIST_SCHEMA_ALL,
                # ARNameType name: specify which form this depends on (ignored
                # with our schemaType)
                None,
                # ARInternalIdList *fieldIdList: filter the schemas by a given
                # set of fields
                None,
                # ARPropList *objPropList: search for specify object properties
                None,

                # (return) ARNameList *nameList: the list of schemas
                byref(schema_list),
                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARNameList(byref(schema_list), arh.FALSE)
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError('Unable to obtain a list of schemas')

        # Save the schema list into the cache
        self.schema_cache = [
            schema_list.nameList[i].value for i in range(schema_list.numItems)
        ]

        self.arlib.FreeARNameList(byref(schema_list), arh.FALSE)
        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

        return self.schema_cache

    def fields(self, schema):
        """Returns a list of field names provided by a selected schema

        :param schema: the schema name to retrieve field names for
        """

        self.update_fields(schema)
        return sorted(self.field_name_cache[schema].keys())

    def query(self, schema, qual, fields):
        """Runs a specified qualification string against a chosen schema and
        returns the all related records with the fields specified by the
        caller.

        :param schema: the schema name to run the query against
        :param qual: the query determining which records to retrieve
        :param fields: a list of field names to retrieve from the schema
        """

        # Ensure we have all field and enum details for the schema
        self.update_fields(schema)

        # Validate that all fields exist.  Note that this is performed here
        # so that we aren't in the middle of allocating memory to the
        # AREntryListFieldList struct when we realise a field is invalid.
        for field in fields:
            if field not in self.field_name_cache[schema]:
                raise ARSError(
                    'A field with name {} does not exist in schema '
                    '{}'.format(field, schema)
                )

        qualifier = arh.ARQualifierStruct()
        qual_cstring = c_char_p(qual)

        if (
            self.arlib.ARLoadARQualifierStruct(
                # ARControlStruct *control: the control record
                byref(self.control),
                # ARNameType schema: the schema to build the qualifier for
                schema,
                # ARNameType displayTag: the name of the form view to use for
                # resolving field names
                None,
                # char *qualString: the qualification string (query) to search
                # with
                qual_cstring,

                # (return) ARQualifierStruct *qualifier: the newly built
                # ARQualifierStruct
                byref(qualifier),
                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARQualifierStruct(byref(qualifier), arh.FALSE)
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError(
                'Unable to load the qualifier using the provided '
                'qualification string for schema {}'.format(schema)
            )

        # Note that we don't run FreeARQualifierStruct here as we need the
        # qualifier for the next call
        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

        field_list = arh.AREntryListFieldList()
        field_list.numItems = len(fields)
        field_list.fieldsList = cast(
            self.clib.malloc(
                field_list.numItems * sizeof(arh.AREntryListFieldStruct)
            ), POINTER(arh.AREntryListFieldStruct)
        )

        # TODO: properly understand the columnWidth and separator fields here
        for i, field in enumerate(fields):
            field_list.fieldsList[i].fieldId = (
                self.field_name_cache[schema][field]
            )
            # From the C API Reference document (Chapter 3 / Entries)
            # For ARGetListEntryWithFields, set this value to a number greater
            # than 0.
            field_list.fieldsList[i].columnWidth = 1
            # From the C API Reference document (Chapter 3 / Entries)
            # For ARGetListEntryWithFields, set this value to one blank space.
            field_list.fieldsList[i].separator = ' '

        num_matches = c_uint()
        entry_list = arh.AREntryListFieldValueList()

        if (
            self.arlib.ARGetListEntryWithFields(
                # ARControlStruct *control: the control record
                byref(self.control),
                # ARNameType schema: the schema to get entries for
                schema,
                # ARQualifierStruct *qualifier: a query specifying entries to
                # retrieve
                byref(qualifier),
                # AREntryListFieldList *getListFields: a list of fields to
                # retrieve with each entry
                byref(field_list),
                # ARSortList *sortList: list of fields to sort results by
                # (NULL for default sort)
                None,
                # unsigned int firstRetrieve: the first record to retrieve
                arh.AR_START_WITH_FIRST_ENTRY,
                # unsigned int maxRetrieve: the maximum number of items to
                # retrieve
                arh.AR_NO_MAX_LIST_RETRIEVE,
                # ARBoolean useLocale: whether to search based on locale
                arh.FALSE,

                # (return) AREntryListFieldValueList *entryList: the entries
                # retrieved
                byref(entry_list),
                # (return) unsigned int numMatches: the number of entries
                # retrieved
                byref(num_matches),
                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARQualifierStruct(byref(qualifier), arh.FALSE)
            self.arlib.FreeAREntryListFieldList(byref(field_list), arh.FALSE)
            self.arlib.FreeAREntryListFieldValueList(
                byref(entry_list), arh.FALSE
            )
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError(
                'Unable to obtain a list of entries using the provided '
                'qualification string for schema {}'.format(schema)
            )

        entries = []

        for i in range(entry_list.numItems):
            # Entries containing more than one id are not supported
            # (ids are supposed to be unique aren't they?)
            if entry_list.entryList[i].entryId.numItems != 1:
                self.arlib.FreeARQualifierStruct(byref(qualifier), arh.FALSE)
                self.arlib.FreeAREntryListFieldList(
                    byref(field_list), arh.FALSE
                )
                self.arlib.FreeAREntryListFieldValueList(
                    byref(entry_list), arh.FALSE
                )
                self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
                raise ARSError(
                    'One or more entries contained multiple IDs that are not '
                    'supported by PyRemedy'
                )

            # Extract the entry id and create an empty dict for the values
            entry_id = entry_list.entryList[i].entryId.entryIdList[0].value
            entry_values = {}

            # Grab the values list for the entry
            values_list = entry_list.entryList[i].entryValues.contents

            for j in range(values_list.numItems):
                field_id = values_list.fieldValueList[j].fieldId
                field_name = self.field_name_cache_rev[schema][field_id]
                data_type = values_list.fieldValueList[j].value.dataType

                # Extract the appropriate piece of data depending on its type
                if data_type == arh.AR_DATA_TYPE_NULL:
                    entry_values[field_name] = None
                elif data_type == arh.AR_DATA_TYPE_CHAR:
                    entry_values[field_name] = (
                        str(values_list.fieldValueList[j].value.u.charVal)
                    )
                elif data_type == arh.AR_DATA_TYPE_ENUM:
                    entry_values[field_name] = (
                        self.field_enum_cache[schema][field_id][
                            values_list.fieldValueList[j].value.u.enumVal
                        ]
                    )
                elif data_type == arh.AR_DATA_TYPE_TIME:
                    entry_values[field_name] = datetime.fromtimestamp(
                        values_list.fieldValueList[j].value.u.timeVal
                    )
                else:
                    self.arlib.FreeARQualifierStruct(
                        byref(qualifier), arh.FALSE
                    )
                    self.arlib.FreeAREntryListFieldList(
                        byref(field_list), arh.FALSE
                    )
                    self.arlib.FreeAREntryListFieldValueList(
                        byref(entry_list), arh.FALSE
                    )
                    self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
                    raise ARSError(
                        'An unknown data type was encountered for field name '
                        '{} on schema {}'.format(field_name, schema)
                    )
                entries.append((entry_id, entry_values))

        self.arlib.FreeARQualifierStruct(byref(qualifier), arh.FALSE)
        self.arlib.FreeAREntryListFieldList(byref(field_list), arh.FALSE)
        self.arlib.FreeAREntryListFieldValueList(byref(entry_list), arh.FALSE)
        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

        return entries

    def update_fields(self, schema):
        """Determines the field IDs for all data fields on a chosen schema and
        then retrieves the related field names and enum mappings.  This method
        assumes that all field names are unique.

        :param schema: the schema name to retrieve field information for
        """

        # Use the cache if possible
        if schema in self.field_name_cache and schema in self.field_enum_cache:
            return

        # Retrieve a list of IDs for a given form
        field_id_list = arh.ARInternalIdList()

        if (
            self.arlib.ARGetListField(
                # ARControlStruct *control: the control record
                byref(self.control),
                # ARNameType schema: the schema to get field ids for
                schema,
                # unsigned long fieldType: bitmask indicating what field types
                # we want
                arh.AR_FIELD_TYPE_DATA,
                # ARTimestamp changedSince: retrieves fields with any
                # modification timestamp
                0,
                # ARPropList objPropList: object properties to search for
                None,

                # (return) ARInternalIdList *idList: the retrieved id list
                byref(field_id_list),
                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARInternalIdList(byref(field_id_list), arh.FALSE)
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError(
                'Unable to obtain field ids for schema {}'.format(schema)
            )

        # Note that we don't run FreeARInternalIdList here as we need the
        # field_id_list for the next call
        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

        field_name_list = arh.ARNameList()
        field_exist_list = arh.ARBooleanList()
        field_limits_list = arh.ARFieldLimitList()

        if (
            self.arlib.ARGetMultipleFields(
                # ARControlStruct *control: the control record
                byref(self.control),
                # ARNameType schema: the scehma to get fields for
                schema,
                # ARInternalIdList *fieldId: the field ids to retrieve
                byref(field_id_list),

                # (return) ARBooleanList *existList: whether the fields exist
                # or not
                byref(field_exist_list),
                # (return) ARInternalIdList *fieldId2: the internal ids
                # retrieved
                None,
                # (return) ARNameList *fieldName: the field names
                byref(field_name_list),
                # (return) ARFieldMappingList *fieldMap: a mapping to the
                # underlying form which to retrieve fields
                None,
                # (return) ARUnsignedIntList *dataType: field data types
                None,
                # (return) ARUnsignedIntList *option: flags indicating whether
                # users must enter values in the form
                None,
                # (return) ARUnsignedIntList *createMode: flags that specify
                # the permission of fields
                None,
                # (return) ARUnsignedIntList *fieldOption: a list of bitmasks
                # indicating whether the field is to be audited or copied when
                # other fields are audited
                None,
                # (return) ARValueList *defaultVal: default field values
                None,
                # (return) ARPermissionListList *assignedGroupListList: groups
                # that have direct permission to fields
                None,
                # (return) ARPermissionListList *permissions: groups that can
                # access the fields
                None,
                # (return) ARFieldLimitList *limit: value limits fo fields
                byref(field_limits_list),
                # (return) ARDisplayInstanceListList *dInstanceList: display
                # properties
                None,
                # (return) ARTextStringList *helpText: help text
                None,
                # (return) ARTimestampList *timestamp: last modified timestamps
                None,
                # (return) ARAccessNameList *owner: the owner of fields
                None,
                # (return) ARAccessNameList *lastChanged: the user that made
                # the last change to the fields
                None,
                # (return) ARTextStringList *changeDiary: a list of change
                # entries
                None,
                # (return) ARPropListList *objPropListList: server properties
                # for fields
                None,
                # (return) ARStatusList *status: notes, warnings or errors
                # generated by the operation
                byref(self.status)
            ) >= arh.AR_RETURN_ERROR
        ):
            self._update_errors()
            self.arlib.FreeARInternalIdList(byref(field_id_list), arh.FALSE)
            self.arlib.FreeARBooleanList(byref(field_exist_list), arh.FALSE)
            self.arlib.FreeARNameList(byref(field_name_list), arh.FALSE)
            self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
            raise ARSError(
                'Unable to obtain field information for schema '
                '{}'.format(schema)
            )

        # Initialise the name and enum caches for this schema
        self.field_name_cache[schema] = {}
        self.field_name_cache_rev[schema] = {}
        self.field_enum_cache[schema] = {}

        for i in range(field_id_list.numItems):
            # Save the field name to id mapping in the cache
            field_id = field_id_list.internalIdList[i]
            field_name = field_name_list.nameList[i].value
            data_type = field_limits_list.fieldLimitList[i].dataType

            # Save the field name to id mapping in the cache
            self.field_name_cache[schema][field_name] = field_id

            # Retrieve enum values if this field is an enum type
            if data_type == arh.AR_DATA_TYPE_ENUM:
                # Initialise the enum entries for this field
                self.field_enum_cache[schema][field_id] = {}

                field_enum_limits_list = (
                    field_limits_list.fieldLimitList[i].u.enumLimits
                )
                field_style = field_enum_limits_list.listStyle

                # Process regular enums mappings
                if field_style == arh.AR_ENUM_STYLE_REGULAR:
                    regular_list = field_enum_limits_list.u.regularList
                    for j in range(regular_list.numItems):
                        enum_id = j
                        enum_value = regular_list.nameList[j].value
                        self.field_enum_cache[schema][field_id][enum_id] = (
                            enum_value
                        )

                # Process custom enums mappings
                elif field_style == arh.AR_ENUM_STYLE_CUSTOM:
                    custom_list = field_enum_limits_list.u.customList
                    for j in range(custom_list.numItems):
                        enum_id = custom_list.enumItemList[j].itemNumber
                        enum_value = custom_list.enumItemList[j].itemName
                        self.field_enum_cache[schema][field_id][enum_id] = (
                            enum_value
                        )

                # Process query enums mappings
                else:
                    # TODO: Implement query enums if possible
                    # query_list = field_enum_limits_list.u.queryList
                    # print("schema: %s" % query_list.schema)
                    # print("server: %s" % query_list.server)
                    # qualifier: query_list.qualifier
                    # print("nameField: %d" % query_list.nameField)
                    # print("numberField: %d" % query_list.numberField)

                    self.arlib.FreeARInternalIdList(
                        byref(field_id_list), arh.FALSE
                    )
                    self.arlib.FreeARBooleanList(
                        byref(field_exist_list), arh.FALSE
                    )
                    self.arlib.FreeARNameList(
                        byref(field_name_list), arh.FALSE
                    )
                    self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)
                    raise ARSError(
                        'The field id {} for schema {} is a query enum which '
                        'is not supported by PyRemedy'.format(field_id, schema)
                    )

        self.arlib.FreeARInternalIdList(byref(field_id_list), arh.FALSE)
        self.arlib.FreeARBooleanList(byref(field_exist_list), arh.FALSE)
        self.arlib.FreeARNameList(byref(field_name_list), arh.FALSE)
        self.arlib.FreeARStatusList(byref(self.status), arh.FALSE)

        self.field_name_cache_rev[schema] = {
            i: n for n, i in self.field_name_cache[schema].iteritems()
        }

    def _update_errors(self):
        """Updates the errors attribute with any errors that occurred on the
        last operation based on the status struct
        """

        # Clear previous errors
        self.errors = []

        # Go through each error present and add them to the errors list
        for i in range(self.status.numItems):
            message_number = self.status.statusList[i].messageNum
            message_text = str(self.status.statusList[i].messageText)
            appended_text = None

            if self.status.statusList[i].appendedText:
                appended_text = str(self.status.statusList[i].appendedText)

            self.errors.append((message_number, message_text, appended_text))
