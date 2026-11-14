# Live validation: first contact (2026-11-14)

**Question.** Can the server open a conversation with a seller and store the first
message through the mobile API, instead of relying on the user's phone?

**Answer.** Yes. One call does both. The path previously implemented in this
server (`create-conversation` followed by a gateway `reply`) never stored the
text, which is why every first contact ended as a partial write.

## How the contract was established

The official Android client was decompiled and read locally:

- `ebk/data/remote/api_commands/MessageBoxApiCommands.java` lists every messagebox
  route. The relevant one is `POST api/users/{userId}/conversations` with a
  `ReplyToSellerDetails` body.
- `ebk/data/entities/payloads/ReplyToSellerDetailsPayloadSerializer.java` prints the
  JSON it builds: `message`, `adId`, plus every non-empty contact field flattened
  to the top level.
- `ReplyToSellerDetails` types `adId` as a **String**.

The API answers with German validation messages, so the remaining field names were
confirmed by iteration rather than guesswork:

| Body | Response |
| --- | --- |
| `{"message": "…", "adId": "<id>"}` | `400 {"errors":[{"message":"Bitte gib deinen Namen ein."}]}` |
| `{"message": "…", "adId": "<id>", "name": "…"}` | `400` (field ignored) |
| `{"message": "…", "adId": "<id>", "senderName": "…"}` | `400` (field ignored) |
| `{"message": "…", "adId": "<id>", "contactName": "…"}` | `200 {"conversationId":"…","existing":false}` |

## Verified request

```text
POST https://api.kleinanzeigen.de/api/users/<uid>/conversations
{"message": "<text>", "adId": "<ad id as JSON string>", "contactName": "<name>"}
```

Followed by one read-back (read only, never used to send):

```text
PUT https://gateway.kleinanzeigen.de/messagebox/api/users/<uid>/conversations/<id>?contentWarnings=true
```

Result: `200`, the thread carries one `OUTBOUND` message whose text matches the
sent text, and the thread appears in the inbox listing. The read-back succeeded
immediately; no propagation delay was observed on this path (unlike the old
create-conversation id, which answered 404 indefinitely).

## Dead ends (do not retry)

- `POST /api/users/<uid>/create-conversation/<ad id>?contactName=…` then a gateway
  `reply`: the create returns an id the gateway does not know. Reading it answers
  `404`, the thread never enters `conversations()`, and the text is not stored.
  A thread that later appears was sent by the user from the app, not by the API.
- `POST /messagebox/api/users/<uid>/conversations` on the gateway: `500`, and
  `500 "Ad not found: 0"` without the ad id — this is a different, non-working route.
- `application/x-www-form-urlencoded` bodies: `415 Unsupported Media Type`.

## Follow-up in this repository

`contact_seller` now sends through the verified call and only reports
`message_sent: true` after the read-back contains our text. Failures after an
acknowledged send surface as `PARTIAL_WRITE` with
`phase=conversation_created_message_unverified`, the known `conversation_id` and
`message_status="unknown"`. The offline suite covers the success path, the
read-back mismatch, an unreadable thread, an unusable acknowledgement and deadline
handling.
