# Code review notes — shared reader (2026-10-19)

This file records the review results for the shared reader
(qdrant_memory/shared_reader.py, qdrant_memory/shared_protocol.py) and the small changes
in shared_client.py and shared_service.py. Each item below was reproduced in the code
before it was accepted. The tests in tests/standalone/test_shared_reader.py pin the
resolutions. The notes are for contributors who change these files.

## Credential validation

- The first version accepted a URL that only started with the loopback prefix. A
  userinfo trick, a missing port, a path or a query string passed the check.
- Resolution: shared_protocol.py supplies one validator. The reader and the client both
  use it. The validator parses the URL, then it checks the address, the port and the
  path. A bad value gives a short error without the input text.

## Record fields

- The version field was typed as an integer, but the owner writes a UUID string. The
  reader therefore lost the revision value of each real record.
- Resolution: the field is a string. The reader accepts a non-empty string only.
- The generated repr of a credential printed the token, and the repr of a record printed
  the memory text. Resolution: both classes supply a repr that hides these values.

## Numeric values

- The score check accepted a boolean, NaN and infinity. A large integer caused an
  overflow. The limit check accepted a boolean.
- Resolution: the reader rejects booleans, requires a finite number and catches the
  conversion errors. The limit must be an integer in the permitted range.

## Duplicate records

- The reader kept the first copy of a duplicate record, although a later copy had a
  higher score.
- Resolution: the reader keeps the copy with the highest score.

## Input limits

- The reader had no limit for the response body, the number of rows or the text length.
- Resolution: the client reads the body with a hard byte limit and without decoding, so
  a compressed body cannot expand it. The reader limits the rows for each space and the
  text length in UTF-8 bytes. A record above the limit is dropped.

## Error handling

- A response with the key "error" and the value null passed the check. A non-dictionary
  error value caused an unexpected error.
- Resolution: any error key fails the search, and the client uses the error object only
  when it is a dictionary.
- Text with an unpaired surrogate caused an encoding error. Resolution: the reader drops
  this record.
- A deeply nested response could raise RecursionError on Python 3.11 even below the
  byte limit. Resolution: convert JSON ValueError and RecursionError to the same
  redacted ValueError. Decoders that accept the nested array still reject its envelope.
- urllib parser errors for malformed IPv6 and Unicode authorities included the input.
  Resolution: normalize these failures to the redacted loopback URL error. Regression
  cases were reproduced before the fix. URL restrictions remain unchanged.

## Tests

- The first test set repeated the implementation. It did not check the request name, the
  credential of each space or the real record shape. It used a version value that the
  owner cannot produce.
- Resolution: the tests state the contract. They check the request name and the
  credential for each space. They use the real record shape. They also cover the
  URL cases, the number cases, the duplicate case, the size limits and the error
  paths.
