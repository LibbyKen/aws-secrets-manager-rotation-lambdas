# Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json
import logging
import os
import influxdb_client
import re
from contextlib import contextmanager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALL_PERMISSION_TYPES = [
    "authorizations",
    "buckets",
    "dashboards",
    "orgs",
    "sources",
    "tasks",
    "telegrafs",
    "users",
    "variables",
    "scrapers",
    "secrets",
    "labels",
    "views",
    "documents",
    "notificationRules",
    "notificationEndpoints",
    "checks",
    "dbrp",
    "notebooks",
    "annotations",
    "remotes",
    "replications",
]


def lambda_handler(event, context):
    """Secrets Manager InfluxDB Token Rotation Handler

    This handler uses the alternating users rotation strategy to rotate an InfluxDB authentication token. This rotation
    scheme authenticates the current token in the InfluxDB instance and creates a new token with the same
    permissions. The new token is then authenticated and verified to have the same properties as the previous token.
    The old token is then deleted, and the rotation is complete. InfluxDB client does not support setting a custom
    value for a token, so the createSecret and setSecret events both take place during the createSecret step.

    Token creation can be achieved for non-operator tokens if you set the Lambda environment variable
    AUTHENTICATION_CREATION_ENABLED to true.

    The Secret SecretString is expected to be a JSON string with the following format:
    {
        'engine': <required: must be set to 'timestream-influxdb'>,
        'org': <required: organization to associate token with>,
        'adminSecretArn': <required: ARN of the admin secret>,
        'type': <required: 'allAccess' or 'operator' or 'custom'>,
        'dbIdentifier': <required: DB identifier>,
        'token': <required unless generating a new token: token being rotated>,
        'writeBuckets': <optional: list of bucket IDs for custom type token, for example ["id1","id2"]>,
        'readBuckets': <optional: list of bucket IDs for custom type token, for example ["id1","id2"]>,
        'permissions': <optional: list of permissions for custom type token, for example ["write-tasks","read-tasks"]>
    }

    Args:
        event (dict): Lambda dictionary of event parameters. These keys must include the following:
            - SecretId: The secret ARN or identifier
            - ClientRequestToken: The ClientRequestToken of the secret version
            - Step: The rotation step (one of createSecret, setSecret, testSecret, or finishSecret)

        context (LambdaContext): The Lambda runtime information

    Raises:
        ResourceNotFoundException: If the secret with the specified ARN and stage does not exist
        ValueError: If the secret is not properly configured for rotation

    """
    arn = event["SecretId"]
    version_token = event["ClientRequestToken"]
    step = event["Step"]

    boto_session = boto3.Session()
    secrets_client = boto_session.client("secretsmanager", endpoint_url=os.environ["SECRETS_MANAGER_ENDPOINT"])
    influxdb_client = boto_session.client("timestream-influxdb")

    create_auth_enabled = False

    # By default token creation is not enabled
    if "AUTHENTICATION_CREATION_ENABLED" in os.environ and os.environ["AUTHENTICATION_CREATION_ENABLED"].lower() == "true":
        create_auth_enabled = True

    # Make sure the version is staged correctly
    metadata = secrets_client.describe_secret(SecretId=arn)
    if "RotationEnabled" in metadata and not metadata["RotationEnabled"]:
        logger.error("Secret %s is not enabled for rotation." % arn)
        raise ValueError("Secret %s is not enabled for rotation." % arn)
    versions = metadata["VersionIdsToStages"]
    if version_token not in versions:
        logger.error("Secret version %s has no stage for rotation of secret %s." % (version_token, arn))
        raise ValueError("Secret version %s has no stage for rotation of secret %s." % (version_token, arn))
    if "AWSCURRENT" in versions[version_token]:
        logger.info("Secret version %s already set as AWSCURRENT for secret %s." % (version_token, arn))
        return
    elif "AWSPENDING" not in versions[version_token]:
        logger.error("Secret version %s not set as AWSPENDING for rotation of secret %s." % (version_token, arn))
        raise ValueError("Secret version %s not set as AWSPENDING for rotation of secret %s." % (version_token, arn))

    if step == "createSecret":
        create_secret(secrets_client, influxdb_client, arn, version_token, create_auth_enabled)

    elif step == "setSecret":
        set_secret(secrets_client, influxdb_client, arn, version_token)

    elif step == "testSecret":
        test_secret(secrets_client, influxdb_client, arn, version_token)

    elif step == "finishSecret":
        finish_secret(secrets_client, influxdb_client, arn, version_token)

    else:
        logger.error("lambda_handler: Invalid step parameter %s for secret %s." % (step, arn))
        raise ValueError("Invalid step parameter %s for secret %s." % (step, arn))


def create_secret(secrets_client, influxdb_client, arn, version_token, create_auth_enabled):
    """Create the secret

    This method first checks for the existence of a secret for the passed in token. If one does not exist,
    it will generate a new token in the InfluxDB instance if authentication creation is enabled, and put the new
    value in the AWSPENDING secret value. This function completes both the createSecret and setSecret steps.

    Args:
        secrets_client (client): The Secrets Manager service client
        influxdb_client (client): The InfluxDB client
        arn (string): The secret ARN or other identifier
        version_token (string): The ClientRequestToken associated with the secret version
        create_auth_enabled (boolean): Flag for if authentication creation is enabled

    Raises:
        ResourceNotFoundException: If the secret with the specified ARN and stage does not exist
        ValueError: If Secrets Manager fails to create a new token

    """

    # Make sure the current secret exists
    current_secret_dict = get_secret_dict(secrets_client, arn, "AWSCURRENT")

    # Now try to get the secret token, if that fails, put a new secret
    try:
        get_secret_dict(secrets_client, arn, "AWSPENDING", version_token)
        logger.info("create_secret: Successfully retrieved secret for %s." % arn)
    except secrets_client.exceptions.ResourceNotFoundException:
        endpoint_url = get_db_endpoint(current_secret_dict["dbIdentifier"], influxdb_client)

        # Before we do anything, ensure we can make a valid connection with the current secret credentials
        if "token" in current_secret_dict:
            with get_connection(endpoint_url, current_secret_dict, arn, "createSecret") as current_conn:
                current_conn.organizations_api().find_organizations()

        if "token" not in current_secret_dict and not create_auth_enabled:
           raise ValueError("Authentication creation has not been enabled to allow the Lambda function to create tokens.")

        admin_secret_dict = get_admin_dict(secrets_client, current_secret_dict, "AWSCURRENT", arn)
        with get_connection(endpoint_url, admin_secret_dict, arn, "createSecret") as conn:
            org = conn.organizations_api().find_organizations(org=current_secret_dict["org"])[0]

            # If there is already a token already defined, copy the existing permission set
            if "token" in current_secret_dict:
                token_perms = get_current_token_perms(conn, current_secret_dict)
            # Token creation can be done for non-operator tokens when the
            # AUTHENTICATION_CREATION_ENABLED environment variable is set to true
            elif create_auth_enabled and current_secret_dict["type"] != "operator":
                if current_secret_dict["type"] == "allAccess":
                    token_perms = create_all_access_token_perms(org.id, conn.users_api().me().id)
                else:
                    token_perms = create_custom_token_perms(current_secret_dict, org.id)
            else:
                raise ValueError("Failed to copy or create a new token.")

            create_token(conn, current_secret_dict, token_perms, org)
        try:
            secrets_client.put_secret_value(SecretId=arn, ClientRequestToken=version_token, SecretString=json.dumps(current_secret_dict), VersionStages=["AWSPENDING"])
        except:
            delete_orphaned_token(endpoint_url, admin_secret_dict, arn, "createSecret", current_secret_dict)
            raise

    logger.info("create_secret: Successfully created new authorization for ARN %s and version %s." % (arn, version_token))


def set_secret(secrets_client, influxdb_client, arn, version_token):
    """Set the secret

    This method authenticates the tokens in the AWSCURRENT and AWSPENDING stages against the InfluxDB instance. Once
    both tokens have been authenticated and the users the tokens belong to is verified to be the same.

    Args:
        secrets_client (client): The Secrets Manager service client
        influxdb_client (client): The InfluxDB client
        arn (string): The secret ARN or other identifier
        version_token (string): The ClientRequestToken associated with the secret version

    Raises: ValueError: If the current and pending token permissions or users are not identical.

    """
    current_secret_dict = get_secret_dict(secrets_client, arn, "AWSCURRENT")
    pending_secret_dict = get_secret_dict(secrets_client, arn, "AWSPENDING", version_token)
    admin_secret_dict = get_admin_dict(secrets_client, pending_secret_dict, "AWSCURRENT", arn)
    endpoint_url = get_db_endpoint(pending_secret_dict["dbIdentifier"], influxdb_client)

    # Only do comparison testing if there is a token in current and pending
    if "token" in current_secret_dict:
        with get_connection(endpoint_url, admin_secret_dict, arn, "setSecret") as operator_client:
            authorizations = operator_client.authorizations_api().find_authorizations()
            current_auth = next((auth for auth in authorizations if auth.token == current_secret_dict["token"]), None)
            pending_auth = next((auth for auth in authorizations if auth.token == pending_secret_dict["token"]))

            # Validate current and pending tokens have the same users
            if current_auth is not None and (not current_auth.user == pending_auth.user):
                delete_orphaned_token(endpoint_url, admin_secret_dict, arn, "setSecret", pending_secret_dict)
                raise ValueError("Current and pending tokens failed user equality test for secret ARN %s." % arn)

            # Validate that the current and pending secret dicts share the same DB identifier
            if current_secret_dict["dbIdentifier"] != pending_secret_dict["dbIdentifier"]:
                delete_orphaned_token(endpoint_url, admin_secret_dict, arn, "setSecret", pending_secret_dict)
                raise ValueError("Rotation is attempting to use a new DB instance for rotating token for secret ARN %s." % arn)

            # Validate current and pending secrets have the same type
            if current_secret_dict["type"] != pending_secret_dict["type"]:
                delete_orphaned_token(endpoint_url, admin_secret_dict, arn, "setSecret", pending_secret_dict)
                raise ValueError("Current and pending secrets do not have the same token types for secret ARN %s." % arn)

    logger.info("set_secret: Successfully completed.")


def test_secret(secrets_client, influxdb_client, arn, version_token):
    """Test the token against the InfluxDB instance

    This method authenticates the tokens in the AWSCURRENT and AWSPENDING stages against the InfluxDB instance. Once
    both tokens have been authenticated and the users the tokens belong to is verified to be the same.

    Args:
        secrets_client (client): The Secrets Manager service client
        influxdb_client (client): The InfluxDB client
        arn (string): The secret ARN or other identifier
        version_token (string): The ClientRequestToken associated with the secret version

    Raises: ValueError: If the pending token fails to authenticate.

    """

    pending_secret_dict = get_secret_dict(secrets_client, arn, "AWSPENDING", version_token)
    endpoint_url = get_db_endpoint(pending_secret_dict["dbIdentifier"], influxdb_client)

    # Verify pending authentication can successfully authenticate
    with get_connection(endpoint_url, pending_secret_dict, arn, "testSecret") as pending_conn:
        pending_conn.organizations_api().find_organizations()

    logger.info("test_secret: Successfully tested token rotation.")


def finish_secret(secrets_client, influxdb_client, arn, version_token):
    """Finish the secret

    This method finalizes the rotation process by marking the secret version passed in as the AWSCURRENT secret and
    deleting the previous authentication token in the InfluxDB instance.

    Args:
        secrets_client (client): The Secrets Manager service client
        influxdb_client (client): The InfluxDB client
        arn (string): The secret ARN or other identifier
        version_token (string): The ClientRequestToken associated with the secret version

    Raises:
        ValueError: If Secrets Manager fails to delete the previous user token in the InfluxDB instance.

    """
    current_secret_dict = get_secret_dict(secrets_client, arn, "AWSCURRENT")
    pending_secret_dict = get_secret_dict(secrets_client, arn, "AWSPENDING", version_token)

    endpoint_url = get_db_endpoint(pending_secret_dict["dbIdentifier"], influxdb_client)
    admin_secret_dict = get_admin_dict(secrets_client, pending_secret_dict, "AWSCURRENT", arn)

    # First describe the secret to get the current version
    metadata = secrets_client.describe_secret(SecretId=arn)
    current_version = None
    for version in metadata["VersionIdsToStages"]:
        if "AWSCURRENT" in metadata["VersionIdsToStages"][version]:
            if version == version_token:
                # The correct version is already marked as current, return
                logger.info("finish_secret: Version %s already marked as AWSCURRENT for %s" % (version, arn))
                return
            current_version = version
            break

    # Finalize by staging the secret version current
    secrets_client.update_secret_version_stage(SecretId=arn, VersionStage="AWSCURRENT", MoveToVersionId=version_token, RemoveFromVersionId=current_version)

    # Delete previous authorization for user if a previous token exists
    if "token" in current_secret_dict:
        with get_connection(endpoint_url, admin_secret_dict, arn, "finishSecret") as operator_client:
            authorizations = operator_client.authorizations_api().find_authorizations()
            current_auth = next((auth for auth in authorizations if auth.token == current_secret_dict["token"]))
            operator_client.authorizations_api().delete_authorization(current_auth)

    logger.info("finish_secret: Successfully set AWSCURRENT stage to version %s for secret %s." % (version_token, arn))


def get_secret_dict(secrets_client, arn, stage, version_token=None):
    """Gets the secret dictionary corresponding for the secret ARN, stage, and token

    This helper function gets credentials for the ARN and stage passed in and returns the dictionary by parsing the
    JSON string

    Args:
        secrets_client (client): The Secrets Manager service client
        arn (string): The secret ARN or other identifier
        stage (string): The stage identifying the secret version
        versionId (string): The ClientRequestToken associated with the secret version, or None if no validation is desired

    Returns:
        SecretDictionary: Secret dictionary

    Raises:
        ResourceNotFoundException: If the secret with the specified ARN and stage does not exist
        ValueError: If the secret is not valid JSON
        KeyError: If required keys missing in secret or engine is not 'timestream-influxdb'

    """

    # Only do VersionId validation against the stage if a token is passed in
    if version_token:
        secret = secrets_client.get_secret_value(SecretId=arn, VersionId=version_token, VersionStage=stage)
    else:
        secret = secrets_client.get_secret_value(SecretId=arn, VersionStage=stage)
    plaintext = secret["SecretString"]
    try:
        secret_dict = json.loads(plaintext)
    except Exception:
        # wrap JSON parser exceptions to avoid possible token disclosure
        logger.error("Invalid secret value JSON for secret %s." % arn)
        raise ValueError("Invalid secret value JSON for secret %s." % arn)

    # Run semantic validations for secrets

    required_fields = ["engine", "type", "dbIdentifier", "org"]

    for field in required_fields:
        if field not in secret_dict:
            raise KeyError("%s key is missing from secret JSON." % field)

    if secret_dict["type"] == "operator" and "token" not in secret_dict:
        raise KeyError("The token value must be set when rotating an operator token.")

    if secret_dict["type"] != "operator" and secret_dict["type"] != "allAccess" and secret_dict["type"] != "custom":
        raise KeyError("%s is not a valid token type." % secret_dict["type"])

    if secret_dict["engine"] != "timestream-influxdb":
        raise KeyError("Database engine must be set to 'timestream-influxdb' in order to use this Lambda rotation function.")

    return secret_dict


def get_admin_dict(secrets_client, secret_dict, stage, arn):
    """Gets the secret dictionary for the operator authentication

    This helper function gets credentials for the operator

    Args:
        secrets_client (client): The Secrets Manager service client
        secret_dict (dict): The secret dictionary containing the ARN for the operator secret
        stage (string): The stage identifying the secret version
        arn (string): The secret ARN or other identifier

    Returns:
        OperatorDictionary (dictionary): Operator dictionary

    Raises:
        ResourceNotFoundException: If the secret with the specified ARN and stage does not exist
        ValueError: If the secret is not valid JSON

    """

    required_fields = ["username", "password"]

    admin_secret = secrets_client.get_secret_value(SecretId=secret_dict["adminSecretArn"], VersionStage=stage)
    admin_plaintext = admin_secret["SecretString"]
    try:
        admin_secret_dict = json.loads(admin_plaintext)
    except Exception:
        # wrap JSON parser exceptions to avoid possible token disclosure
        logger.error("Invalid secret value JSON for secret %s." % arn)
        raise ValueError("Invalid secret value JSON for secret %s." % arn)

    # Run validations against the secret
    for field in required_fields:
        if field not in admin_secret_dict:
            raise KeyError("%s key is missing from secret JSON." % field)

    return admin_secret_dict


def get_db_endpoint(db_instance_identifier, influxdb_client):
    """Get InfluxDB information

    This helper function returns the url for the InfluxDB instance
    that matches the identifier which is provided in the user secret.

    Args:
        db_instance_identifier (string): The InfluxDB instance identifier
        influxdb_client (client): The InfluxDB client

    Returns:
        endpoint (string): The endpoint for the DB instance

    Raises:
        KeyError: DB info returned does not contain expected key

    """

    describe_response = influxdb_client.get_db_instance(identifier=db_instance_identifier)

    if describe_response is None or describe_response["endpoint"] is None:
        raise KeyError("Invalid endpoint info for InfluxDB instance.")

    return describe_response["endpoint"]


@contextmanager
def get_connection(endpoint_url, secret_dict, arn, step):
    """Get connection to InfluxDB

    This helper function returns a connection to the provided InfluxDB instance.

    Args:
        endpoint_url (string): Url for the InfluxDB instance
        secret_dict (dictionary): Dictionary with either username/password or token to authenticate connection
        arn (string): ARN for secret to log in event of failure to make connection
        step (string): Step in which the Lambda function is making the connection

    Raises:
        ValueError: If the connection or health check fails

    """
    conn = None
    try:
        conn = (
            influxdb_client.InfluxDBClient(url="https://" + endpoint_url + ":8086", token=secret_dict["token"], debug=False, verify_ssl=True)
            if "token" in secret_dict
            else influxdb_client.InfluxDBClient(url="https://" + endpoint_url + ":8086", username=secret_dict["username"], password=secret_dict["password"], debug=False, verify_ssl=True)
        )

        # Verify InfluxDB connection
        health = conn.ping()
        if not health:
            logger.error("%s: Connection failure." % step)

        yield conn
    except Exception as err:
        logger.error("%s: Connection failure with secret ARN %s %s." % (step, arn, err))
        raise ValueError("%s: Failed to set new authorization with secret ARN %s %s." % (step, arn, err)) from err
    finally:
        if conn is not None:
            conn.close()


def delete_orphaned_token(
    endpoint_url, admin_secret_dict, arn, step, pending_secret_dict
):
    """Delete a token that could not be rotated

    This method is used for cleaning up tokens that have failed the required validations for a successful rotation.

    Args:
        endpoint_url (string): The url for the DB instance
        admin_secret_dict (dictionary): The dictionary used to authorize the orphaned token deletion
        arn (string): The secret ARN or other identifier
        step (string): The current step for rotation
        pending_secret_dict(dictionary): The dictionary which holds the token to be deleted

    """

    with get_connection(endpoint_url, admin_secret_dict, arn, step) as operator_client:
        authorizations = operator_client.authorizations_api().find_authorizations()
        pending_auth = next((auth for auth in authorizations if auth.token == pending_secret_dict["token"]))
        operator_client.authorizations_api().delete_authorization(pending_auth)


def append_organization_scoped_permission(token_perms, perm_type, action, org_id):
    """

    Append permissions to the permission list with the format: "<action>:orgs/<org_id>/<perm_type>"

    Args:
        token_perms (list): List appending the permission to
        perm_type (string): The type value for the permission
        action (string): The action value for the permission
        org_id (string): The organization id

    """

    token_perms.append(
        influxdb_client.Permission(resource=influxdb_client.PermissionResource(type=perm_type, org_id=org_id), action=action)
    )


def append_organization_permission(token_perms, perm_type, action, org_id):
    """

    Append permissions to the permission list with the format: "<action>:/orgs/org_id"

    Args:
        token_perms (list): List appending the permission to
        perm_type (string): The type value for the permission
        action (string): The action value for the permission
        org_id (string): The organization id

    """

    token_perms.append(
        influxdb_client.Permission(resource=influxdb_client.PermissionResource(id=org_id, type=perm_type), action=action)
    )


def append_user_permission(token_perms, perm_type, action, user_id):
    """

    Append permissions to the permission list with the format: "<action>:/users/<user_id>"

    Args:
        token_perms (list): List appending the permission to
        perm_type (string): The type value for the permission
        action (string): The action value for the permission
        user_id (string): The user id

    """
    token_perms.append(
        influxdb_client.Permission(resource=influxdb_client.PermissionResource(id=user_id, type=perm_type), action=action)
    )


def append_organization_and_bucket_scoped_permission(
    token_perms, perm_type, action, org_id, bucket_id
):
    """

    Append permissions to the permission list with the format: "<action>:orgs/<org_id>/buckets/<bucket_id>"

    Args:
        token_perms (list): List appending the permission to
        perm_type (string): The type value for the permission
        action (string): The action value for the permission
        org_id (string): The organization id
        bucket_id (string): The bucket id

    """
    token_perms.append(
        influxdb_client.Permission(resource=influxdb_client.PermissionResource(name=bucket_id, type=perm_type, org_id=org_id), action=action)
    )


def get_current_token_perms(conn, current_secret_dict):
    """

    Gets the permission set of the current token.

    Args:
        conn (InfluxDBClient): The connection to the InfluxDB instance
        current_secret_dict (dictionary): Secret dictionary with token type and permissions

    Raises:
        ValueError: If the token value defined in the secret does not exist in the db instance

    Returns:
        current_token.permissions (list): The current permissions of the current token

    """

    authorizations = conn.authorizations_api().find_authorizations()
    current_auth = next((auth for auth in authorizations if auth.token == current_secret_dict["token"]), None,)

    if current_auth is None:
        raise ValueError("Failed to find the current secret token defined in the InfluxDB instance.")
    else:
        logger.info("Existing token found in DB instance, token will be rotated with existing permissions.")
        return current_auth.permissions


def create_token(conn, current_secret_dict, token_perms, org):
    """

    Create new authorization token in InfluxDB instance and set the token value in the secret dictionary.
    The type and permissions are set from the values defined in the secret dictionary.

    Args:
        conn (InfluxDBClient): The connection to the InfluxDB instance
        current_secret_dict (dictionary): Secret dictionary with token type and permissions
        token_perms (list): Set of permissions to apply to the token being created
        org (Organization): The organization to associate with the token

    """

    # InfluxDB doesn't support setting token values for already created tokens
    # We must therefore create the token in InfluxDB at this stage
    new_authorization = conn.authorizations_api().create_authorization(org_id=org.id, permissions=token_perms)
    current_secret_dict["token"] = new_authorization.token


def create_all_access_token_perms(org_id, user_id):
    """

    Create the set of permissions defined for an allAccess token.

    Args:
        org_id (string): Id for the organization to set permissions
        user_id (string): The id for the user

    Returns:
        token_perms (list): The set of permissions for an allAccess token

    """

    token_perms = []
    # allAccess token is scoped to a specific org and can access all buckets in that organization
    for perm_type in ALL_PERMISSION_TYPES:
        if perm_type == "users":
            append_user_permission(token_perms, perm_type, "read", user_id)
            append_user_permission(token_perms, perm_type, "write", user_id)
        elif perm_type == "orgs":
            # allAccess tokens only get read access for the organization
            append_organization_permission(token_perms, perm_type, "read", org_id)
        else:
            append_organization_scoped_permission(token_perms, perm_type, "read", org_id)
            append_organization_scoped_permission(token_perms, perm_type, "write", org_id)

    return token_perms


def create_custom_token_perms(current_secret_dict, org_id):
    """

    Create the set of permissions defined by the custom permissions set in the secret.

    Args:
        current_secret_dict (dictionary): Set of permissions to set for token
        org_id (string): Id for the organization to set permissions

    Returns:
        token_perms (list): The set of permissions for a custom token

    Raises:
        ValueError: If no permissions were set

    """

    token_perms = []

    if "readBuckets" in current_secret_dict:
        for bucket_id in current_secret_dict["readBuckets"]:
            append_organization_and_bucket_scoped_permission(token_perms, "buckets", "read", org_id, bucket_id)
    if "writeBuckets" in current_secret_dict:
        for bucket_id in current_secret_dict["writeBuckets"]:
            append_organization_and_bucket_scoped_permission(token_perms, "buckets", "write", org_id, bucket_id)
    # Handle permissions list, for example ["write-tasks", "read-tasks"]
    if "permissions" in current_secret_dict:
        for perm in current_secret_dict["permissions"]:
            perm_type = get_type_from_perm_string(perm)
            action = get_action_from_perm_string(perm)
            # A permission of the form "read-orgs" needs to be formatted as
            # "read:/orgs/<org ID>", meaning the permission is to read the organization.
            # Append_organization_permission will format permissions this way.
            if perm_type == "orgs":
                append_organization_permission(token_perms, perm_type, action, org_id)
            else:
                append_organization_scoped_permission(token_perms, perm_type, action, org_id)

    if not token_perms:
        raise ValueError("No permissions were set for token creation.")

    return token_perms


def get_action_from_perm_string(perm_string):
    """

    Parse the string to return the action portion of a permission string. For example, read-users, return read.

    Args:
        perm_string (string): The permission string

    Returns:
        get_perm_string_item (string): The action portion of a permission string

    """

    return get_perm_string_item(perm_string, 0)


def get_type_from_perm_string(perm_string):
    """

    Parse the string to return the type portion of a permission string. For example, read-users, return users.

    Args:
        perm_string (string): The permission string

    Returns:
        get_perm_string_item (string): The type portion of a permission string

   """

    return get_perm_string_item(perm_string, 1)


def get_perm_string_item(perm_string, idx):
    """

    Parse the string to return either the action or type portion of a permission string, where the string has the following format: <action>-<type>

    Args:
        perm_string: The permission string
        idx: The index of the item to retrieve

    Returns:
        perm_string (string): The permission string

    Raises:
        ValueError: If permission string does not match the formation <action>-<type>

    """

    if not re.search("^[a-z][a-z]+-[a-z]+[a-z]$", perm_string):
        raise ValueError("Permission %s must fit the format <action>-<type>, for example write-tasks.")

    return perm_string.split("-", 1)[idx]


