# VisionGuard API Reference

Base URL: `http://<host>:<port>` (default Flask dev server: `http://localhost:5000`)

---

## Authentication

VisionGuard uses two authentication mechanisms:

### Session-based (browser)
Standard login via `/login`. Protected routes redirect unauthenticated users to `/login`.

### API Key (programmatic access)
An API key is generated automatically when a user registers and can be regenerated from `/settings`. Keys are 64-character hex strings.

Pass the key in one of two ways:

```
Authorization: Bearer <api_key>
```
```
X-API-Key: <api_key>
```

---

## JSON API Endpoints

These endpoints are intended for programmatic (machine-to-machine) access.

---

### GET /api/cameras

Returns the list of cameras belonging to the authenticated user.

**Auth:** Session login **or** API key

**Response `200 OK`:**
```json
[
  {
    "id": "cam-001",
    "name": "Front Entrance",
    "rtsp_url": "rtsp://192.168.1.10:554/stream"
  }
]
```

---

### POST /alerts

Receives an alert from a backend detection system (e.g. an ML pipeline).

**Auth:** API key required

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `alert_type` | string | Yes | Type of alert (e.g. `"obstruction"`, `"darkness"`) |
| `detail` | string | Yes | Human-readable description of the alert |
| `camera_source` | string | Yes | Camera identifier the alert originated from |

> Note: `"darkness"` is normalised to `"obstruction"` internally.

**Example request:**
```bash
curl -X POST http://localhost:5000/alerts \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"alert_type": "obstruction", "detail": "Camera view blocked", "camera_source": "cam-001"}'
```

**Response `200 OK`:**
```json
{
  "success": true,
  "alert_type": "obstruction",
  "email_sent": true
}
```

**Error responses:**

| Code | Reason |
|---|---|
| `400` | Missing required field (`alert_type`, `detail`, or `camera_source`) |
| `401` | Missing or invalid API key |

> Email notifications are sent when `email_notifications_enabled` is true for the user and an `"obstruction"` alert is received. There is a per-camera cooldown (default 900 seconds) to prevent flooding.

---

### GET /api/notifications

Returns the 20 most recent notifications for the authenticated user.

**Auth:** Session login required

**Response `200 OK`:**
```json
[
  {
    "id": 1,
    "user_id": 3,
    "message": "Obstruction detected on cam-001",
    "is_read": false,
    "created_at": "2026-05-16T10:30:00"
  }
]
```

---

### DELETE /api/notifications/`<notification_id>`

Deletes a single notification.

**Auth:** Session login required

**Response `200 OK`:**
```json
{ "success": true }
```

---

### DELETE /api/notifications

Deletes all notifications for the authenticated user.

**Auth:** Session login required

**Response `200 OK`:**
```json
{ "success": true }
```

---

## Web / Form Endpoints

These endpoints serve the browser-based UI. They are session-protected and return HTML or redirects rather than JSON.

### Authentication

| Method | Path | Description |
|---|---|---|
| GET / POST | `/login` | User login |
| GET / POST | `/signup` | New user registration (generates API key) |
| GET | `/logout` | Log out current session |

### Pages

| Method | Path | Description |
|---|---|---|
| GET | `/` | Redirects to `/dashboard` or `/login` |
| GET | `/dashboard` | Main dashboard |
| GET | `/cameras` | Camera management page |
| GET | `/camera-groups` | Camera group management page |
| GET | `/reports` | Reports overview |
| GET | `/locations` | Locations page |
| GET | `/areas` | Areas page |
| GET | `/feed` | Live feed page |
| GET / POST | `/settings` | User settings and API key management |

### Camera Management

| Method | Path | Body (form) | Description |
|---|---|---|---|
| POST | `/add_camera` | `location`, `name`, `rtsp_url`, `type`, `group_id`(opt) | Add a camera |
| POST | `/cameras/<camera_id>/edit` | `location`, `name`, `rtsp_url`, `type`, `status`, `group_id`(opt) | Edit a camera |
| POST | `/cameras/<camera_id>/delete` | — | Delete a single camera |
| POST | `/cameras/bulk-delete` | `camera_ids[]` | Delete multiple cameras |
| POST | `/cameras/bulk-edit` | `camera_ids[]`, `location`(opt), `type`(opt), `status`(opt), `group_id`(opt) | Bulk-edit cameras |

> Set `group_id` to `__ungrouped__` in bulk-edit to remove cameras from their group.

### Camera Group Management

| Method | Path | Body (form) | Description |
|---|---|---|---|
| POST | `/camera-groups` | `name`, `description`(opt), `camera_ids[]`(opt) | Create a group |
| POST | `/camera-groups/<group_id>/edit` | `name`, `description`(opt) | Edit a group |
| POST | `/camera-groups/<group_id>/delete` | — | Delete a group (cameras become ungrouped) |
| POST | `/camera-groups/bulk-delete` | `group_ids[]` | Delete multiple groups |
| POST | `/camera-groups/bulk-edit` | `group_ids[]`, `description` | Bulk-update group descriptions |

### Reports & Exports

| Method | Path | Query params | Description |
|---|---|---|---|
| GET | `/reports/cameras/<camera_id>` | `timeframe` | Camera report detail page |
| GET | `/reports/groups/<group_id>` | `timeframe` | Group report detail page |
| GET | `/reports/camera-report.pdf` | `camera_id`, `timeframe` | Download camera report as PDF |
| GET | `/reports/group-report.pdf` | `group_id`, `timeframe` | Download group report as PDF |

`timeframe` accepts: `day`, `week`, `month`, `year`, `5years`, `all`

---

## API Key Management

| Action | How |
|---|---|
| View current key | `GET /settings` → displayed in the Settings page |
| Regenerate key | `POST /settings` with form field `action=regenerate_api_key` |

Regenerating a key immediately invalidates the previous one. Any backend service using the old key must be updated.
