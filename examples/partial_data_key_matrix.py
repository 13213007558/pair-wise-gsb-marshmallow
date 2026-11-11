from marshmallow import Schema, fields, EXCLUDE, RAISE
from marshmallow.exceptions import ValidationError
import json

# ---------------------------------------------------------------------------
# Inner nested schema.  Internal attribute names differ from external data_key.
#   display_name <-> external "displayName"   (the field we want to PATCH)
#   client_id    <-> external "clientId"      (required auth material)
#   client_secret<-> external "clientSecret"  (required auth material)
# ---------------------------------------------------------------------------
class CredentialSchema(Schema):
    display_name = fields.Str(data_key="displayName", required=True)
    client_id = fields.Str(data_key="clientId", required=True)
    client_secret = fields.Str(data_key="clientSecret", required=True)


class AccountSchema(Schema):
    # nested collection; external name differs from internal attribute name
    credentials = fields.Nested(
        CredentialSchema, many=True, data_key="credentialList", required=True
    )
    # a plain aliased required field at top level
    account_name = fields.Str(data_key="accountName", required=True)


def run(label, schema, payload, partial=None):
    try:
        out = schema.load(payload, partial=partial)
        print(f"[ OK ] {label}\n       -> {json.dumps(out, ensure_ascii=False, sort_keys=True)}")
    except ValidationError as e:
        print(f"[ERR ] {label}\n       keys={json.dumps(e.messages, ensure_ascii=False, sort_keys=True)}")
    print()


full = {
    "accountName": "acct-1",
    "credentialList": [
        {"displayName": "prod key", "clientId": "id-1", "clientSecret": "shh"}
    ],
}

print("=" * 78)
print("A. BASELINE: no partial, required enforcement / error key naming")
print("=" * 78)
run("A1 full valid payload", AccountSchema(), full)
run("A2 missing nested required clientSecret (external key omitted)",
    AccountSchema(),
    {"accountName": "acct-1",
     "credentialList": [{"displayName": "prod key", "clientId": "id-1"}]})
run("A3 explicit null for required clientSecret",
    AccountSchema(),
    {"accountName": "acct-1",
     "credentialList": [{"displayName": "prod key", "clientId": "id-1",
                         "clientSecret": None}]})
run("A4 whole nested collection missing",
    AccountSchema(), {"accountName": "acct-1"})
run("A5 top-level aliased field missing (accountName omitted)",
    AccountSchema(),
    {"credentialList": [
        {"displayName": "p", "clientId": "i", "clientSecret": "s"}]})

print("=" * 78)
print("B. partial=True: EVERY field (any depth) may be missing")
print("=" * 78)
run("B1 only displayName provided inside nested item",
    AccountSchema(),
    {"credentialList": [{"displayName": "new name"}]}, partial=True)
run("B2 empty object", AccountSchema(), {}, partial=True)
run("B3 explicit null still rejected even with partial=True",
    AccountSchema(),
    {"credentialList": [{"clientSecret": None}]}, partial=True)

print("=" * 78)
print("C. partial=<string paths>: which NAME SPACE are paths interpreted in?")
print("=" * 78)
# Top-level field: internal attr vs external data_key
run("C1 partial internal attr name 'account_name', external omitted",
    AccountSchema(), {"credentialList": [
        {"displayName": "p", "clientId": "i", "clientSecret": "s"}]},
    partial=["account_name"])
run("C2 partial external data_key 'accountName', external omitted",
    AccountSchema(),
    {"credentialList": [
        {"displayName": "p", "clientId": "i", "clientSecret": "s"}]},
    partial=["accountName"])

# Nested field via dotted path: two competing conventions
nested_only_display = {"credentialList": [{"displayName": "new name"}]}
run("C3 dotted path with INTERNAL names 'credentials.display_name'",
    AccountSchema(), nested_only_display,
    partial=["credentials.display_name"])
run("C4 dotted path with EXTERNAL names 'credentialList.displayName'",
    AccountSchema(), nested_only_display,
    partial=["credentialList.displayName"])
run("C5 external path but full nested item supplied (should pass anyway)",
    AccountSchema(), full, partial=["credentialList.displayName"])

# Does partial on only the nested leaf relax the top-level required field?
run("C6 partial only nested leaf; top-level accountName missing -> must error",
    AccountSchema(), nested_only_display,
    partial=["credentials.display_name"])

print("=" * 78)
print("D. only / exclude interaction with required")
print("=" * 78)
class ExcludeSchema(Schema):
    credentials = fields.Nested(
        CredentialSchema, many=True, data_key="credentialList",
        exclude=("client_secret",), required=True)
    account_name = fields.Str(data_key="accountName", required=True)

run("D1 exclude removes client_secret from required set",
    ExcludeSchema(),
    {"accountName": "a",
     "credentialList": [{"displayName": "p", "clientId": "i"}]})

class OnlySchema(Schema):
    credentials = fields.Nested(
        CredentialSchema, many=True, data_key="credentialList",
        only=("display_name",), required=True)
    account_name = fields.Str(data_key="accountName", required=True)

run("D2 only=display_name: clientId/clientSecret no longer required",
    OnlySchema(),
    {"accountName": "a", "credentialList": [{"displayName": "p"}]})
run("D3 only + partial internal path, displayName omitted",
    OnlySchema(),
    {"accountName": "a", "credentialList": [{}]},
    partial=["credentials.display_name"])
run("D4 only + partial EXTERNAL path, displayName omitted",
    OnlySchema(),
    {"accountName": "a", "credentialList": [{}]},
    partial=["credentialList.displayName"])

print("=" * 78)
print("E. many=True error indices and key names inside collection")
print("=" * 78)
run("E1 second item invalid -> index + external data_key",
    AccountSchema(),
    {"accountName": "a",
     "credentialList": [
        {"displayName": "p", "clientId": "i", "clientSecret": "s"},
        {"displayName": "p2"}]})
