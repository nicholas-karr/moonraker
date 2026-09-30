
# Authorization and Authentication

The Authorization endpoints provide access to the various methods
used to authorize connections to Moonraker.  This includes user
authentication, API Key authentication, Temporary access via
"oneshot tokens", and IP and/or domain based authentication
("ie: trusted clients).

Untrusted clients must use either a JSON Web Token or an API key to access
Moonraker's HTTP APIs.  JWTs should be included in the `Authorization`
header as a `Bearer` type for each HTTP request.  If using an API Key it
should be included in the `X-Api-Key` header for each HTTP Request.

Websocket authentication can be achieved via the request itself or
post connection.  Unlike HTTP requests it is not necessary to pass a
token and/or API Key to each request.  The
[identify connection](./server.md#identify-connection) endpoint takes optional
`access_token` and `api_key` parameters that may be used to authenticate
a user already logged in, otherwise the `login` API may be used for
authentication.  Websocket connections will stay authenticated until
the connection is closed or the user logs out.

There is a single user, `biokalico`, whose password is the shared password
set by [simple_password_auth](../configuration.md#simple_password_auth).
User accounts cannot be created, deleted or renamed.

/// note
ECMAScript imposes limitations on certain requests that prohibit the
developer from modifying the HTTP headers (ie: Requests to open a
websocket, "download" requests that open a user dialog).  In these cases
it is recommended for the developer to request a `oneshot_token`, then
send the result via the `token` query string argument in the desired
request.
///

/// warning
It is strongly recommended that arguments for the below APIs are
passed in the request's body.
///

## Login User
```{.http .apirequest title="HTTP Request"}
POST /access/login
Content-Type: application/json

{
    "username": "biokalico",
    "password": "my_password"
}
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.login",
    "params": {
        "username": "biokalico",
        "password": "my_password"
    },
    "id": 1323
}
```

/// api-parameters
    open: True

| Name       |  Type  | Default      | Description                   |
| ---------- | :----: | ------------ | ----------------------------- |
| `username` | string | **REQUIRED** | Always `biokalico`.           |
| `password` | string | **REQUIRED** | The shared password.          |

///

/// collapse-code
```{.json .apiresponse title="Example Response"}
{
    "username": "my_user",
    "token": "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJpc3MiOiAiTW9vbnJha2VyIiwgImlhdCI6IDE2MTg4NzY4MDAuNDgxNjU1LCAiZXhwIjogMTYxODg4MDQwMC40ODE2NTUsICJ1c2VybmFtZSI6ICJteV91c2VyIiwgInRva2VuX3R5cGUiOiAiYXV0aCJ9.QdieeEskrU0FrH7rXKuPDSZxscM54kV_vH60uJqdU9g",
    "refresh_token": "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJpc3MiOiAiTW9vbnJha2VyIiwgImlhdCI6IDE2MTg4NzY4MDAuNDgxNzUxNCwgImV4cCI6IDE2MjY2NTI4MDAuNDgxNzUxNCwgInVzZXJuYW1lIjogIm15X3VzZXIiLCAidG9rZW5fdHlwZSI6ICJyZWZyZXNoIn0.btJF0LJfymInhGJQ2xvPwkp2dFUqwgcw4OA_wE-EcCM",
    "action": "user_logged_in"
}
```
///

/// api-response-spec
    open: True

| Field           |  Type  | Description                                                          |
| --------------- | :----: | -------------------------------------------------------------------- |
| `username`      | string | The name of the logged in user.                                      |
| `token`         | string | A JSON Web Token (JWT) used to authenticate requests, also commonly  |
|                 |        | referred to as an `access token`.  HTTP requests should include this |^
|                 |        | token in the `Authorization` header as a `Bearer` type.  This token  |^
|                 |        | expires after 1 hour.                                                |^
| `refresh_token` | string | A JWT that should be used to generate new access tokens after they   |
|                 |        | expire.  See the [refresh token section](#refresh-json-web-token)    |^
|                 |        | for details.                                                         |^
| `action`        | string | The action taken by the auth manager.  Will always be                |
|                 |        | "user_logged_in".                                                    |^

///

/// note
This endpoint may be accessed without prior authentication.  A 401 will
only be returned if the authentication fails.
///

## Logout Current User

```{.http .apirequest title="HTTP Request"}
POST /access/logout
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.logout",
    "id": 1323
}
```

/// collapse-code
```{.json .apiresponse title="Example Response"}
{
    "username": "my_user",
    "action": "user_logged_out"
}
```
///

/// api-response-spec
    open: True

| Field      |  Type  | Description                                           |
| ---------- | :----: | ----------------------------------------------------- |
| `username` | string | The name of the logged out user.                      |
| `action`   | string | The action taken by the auth manager.  Will always be |
|            |        | "user_logged_out".                                    |^

///

## Refresh JSON Web Token
This endpoint can be used to refresh an expired access token.  If this
request returns an error then the refresh token is no longer valid and
the user must login with their credentials.

```{.http .apirequest title="HTTP Request"}
POST /access/refresh_jwt
Content-Type: application/json

{
    "refresh_token": "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJpc3MiOiAiTW9vbnJha2VyIiwgImlhdCI6IDE2MTg4Nzc0ODUuNzcyMjg5OCwgImV4cCI6IDE2MjY2NTM0ODUuNzcyMjg5OCwgInVzZXJuYW1lIjogInRlc3R1c2VyIiwgInRva2VuX3R5cGUiOiAicmVmcmVzaCJ9.Y5YxGuYSzwJN2WlunxlR7XNa2Y3GWK-2kt-MzHvLbP8"
}
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.refresh_jwt",
    "params": {
        "refresh_token": "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJpc3MiOiAiTW9vbnJha2VyIiwgImlhdCI6IDE2MTg4Nzc0ODUuNzcyMjg5OCwgImV4cCI6IDE2MjY2NTM0ODUuNzcyMjg5OCwgInVzZXJuYW1lIjogInRlc3R1c2VyIiwgInRva2VuX3R5cGUiOiAicmVmcmVzaCJ9.Y5YxGuYSzwJN2WlunxlR7XNa2Y3GWK-2kt-MzHvLbP8"
    },
    "id": 1323
}
```

/// api-parameters
    open: True

| Name            |  Type  | Default      | Description                           |
| --------------- | :----: | ------------ | ------------------------------------- |
| `refresh_token` | string | **REQUIRED** | A valid `refresh_token` for the user. |

///


/// collapse-code
```{.json .apiresponse title="Example Response"}
{
    "username": "my_user",
    "token": "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJpc3MiOiAiTW9vbnJha2VyIiwgImlhdCI6IDE2MTg4NzgyNDMuNTE2Nzc5MiwgImV4cCI6IDE2MTg4ODE4NDMuNTE2Nzc5MiwgInVzZXJuYW1lIjogInRlc3R1c2VyIiwgInRva2VuX3R5cGUiOiAiYXV0aCJ9.Ia_X_pf20RR4RAEXcxalZIOzOBOs2OwearWHfRnTSGU",
    "action": "user_jwt_refresh"
}
```
///

/// api-response-spec
    open: True

| Field      |  Type  | Description                                                          |
| ---------- | :----: | -------------------------------------------------------------------- |
| `username` | string | The username of the entry whose access token ws refreshed.           |
| `token`    | string | A JSON Web Token (JWT) used to authenticate requests, also commonly  |
|            |        | referred to as an `access token`.  HTTP requests should include this |^
|            |        | token in the `Authorization` header as a `Bearer` type.  This token  |^
|            |        | expires after 1 hour.                                                |^
| `action`   | string | The action taken by the Auth Manager.  Will always be                |
|            |        | "user_jwt_refresh".                                                  |^

///

/// note
This endpoint may be accessed by unauthorized clients.  A 401 will
only be returned if the refresh token is invalid.
///

## Generate a Oneshot Token

Javascript is not capable of modifying the headers for some HTTP requests
(for example, the `websocket`), which is a requirement to apply JWT or API Key
authorization.  To work around this clients may request a Oneshot Token and
pass it via the query string for these requests.  Tokens expire in 5 seconds
and may only be used once, making them relatively safe for inclusion in the
query string.

```{.http .apirequest title="HTTP Request"}
GET /access/oneshot_token
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.oneshot_token",
    "id": 1323
}
```

```{.json .apiresponse title="Example Response"}
"APDBEGHUTBUD6SOAYBPF3KE5BRMO7YSL"
```

/// api-response-spec
    open: True

The response is a string value containing the oneshot token. It may
added to a request's query string for access to any API endpoint.  The query
string should be added in the form of:

```
?token={base32_random_token}
```

///

## Get authorization module info

```{.http .apirequest title="HTTP Request"}
GET /access/info
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.info",
    "id": 1323
}
```

/// collapse-code
```{.json .apiresponse title="Example Response"}
{
    "login_required": true,
    "trusted": true
}
```
///

/// api-response-spec
    open: True

| Field            | Type | Description                                        |
| ---------------- | :--: | -------------------------------------------------- |
| `login_required` | bool | Set to `true` once the shared password is in use.  |
| `trusted`        | bool | Set to `true` when the connection making the info  |
|                  |      | request is a trusted connection.                   |^

///

/// note
This endpoint may be accessed by unauthorized clients.
///

## Get the Login Hint

Returns the reminder text shown on Mainsail's login screen, set by the `hint`
option of [simple_password_auth](../configuration.md#simple_password_auth).

```{.http .apirequest title="HTTP Request"}
GET /server/simple_password_auth/hint
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "server.simple_password_auth.hint",
    "id": 1323
}
```

```{.json .apiresponse title="Example Response"}
{
    "password_hint": "ask the lab manager"
}
```

/// api-response-spec
    open: True

| Field           |  Type  | Description                                  |
| --------------- | :----: | -------------------------------------------- |
| `password_hint` | string | The configured hint, or an empty string.     |

///

/// note
This endpoint may be accessed by unauthorized clients.
///

## Get the Current API Key

```{.http .apirequest title="HTTP Request"}
GET /access/api_key
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.get_api_key",
    "id": 1323
}
```

```{.json .apiresponse title="Example Response"}
e514851f37b94c779d955212b6906f95
```

/// api-response-spec
    open: True

The response string value containing the current API key.

///

## Generate a New API Key
```{.http .apirequest title="HTTP Request"}
POST /access/api_key
```

```{.json .apirequest title="JSON-RPC Request"}
{
    "jsonrpc": "2.0",
    "method": "access.post_api_key",
    "id": 1323
}
```

```{.json .apiresponse title="Example Response"}
e514851f37b94c779d955212b6906f95
```

/// api-response-spec
    open: True

The response string value containing the new API key.

///

/// note
After this request executes the API key change is applied immediately.
All subsequent HTTP requests from untrusted clients must use the new key.
Changing the API Key will not affect open websockets authenticated using
the previous API Key.
///
