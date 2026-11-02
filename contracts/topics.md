# Topics and routing

One camera owns one subtree. The builders and parsers live in `src/atms_contracts/topics.py`.

| Layer | Name | Example |
|---|---|---|
| MQTT topic (device) | `atms/{state}/{city}/{road}/{camera}/event/{name}` | `atms/MH/PUNE/NH48/GNT001/event/accident` |
| MQTT topic (health) | `atms/{state}/{city}/{road}/{camera}/status` | `atms/MH/PUNE/NH48/GNT001/status` |
| AMQP routing key on `amq.topic` | the MQTT topic with `/` replaced by `.` | `atms.MH.PUNE.NH48.GNT001.event.accident` |
| Bridge queue binding | `atms.#` | |
| Kafka topic | `incident-events` | |
| Kafka dead-letter topic | `incident-events.dlq` | |
| Kafka message key | road segment `{state}/{city}/{road}` | `MH/PUNE/NH48` |

Rules

- Segments use letters, digits, `_` and `-` only. A dot would break the MQTT to AMQP mapping.
- Devices publish at QoS 1. Consumers are idempotent on `event_id`.
- A camera may publish only under its own subtree. RabbitMQ topic permission for one camera is the pattern returned by `device_publish_pattern`.
- Retained message plus last will on the status topic carries camera online or offline.
- The bridge acks RabbitMQ only after Kafka confirms the write. Invalid events go to the dead-letter topic with the failure reason in a header.
