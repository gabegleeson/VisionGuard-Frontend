import express from "express";
import mysql from "mysql2/promise";

const app = express();
app.use(express.json());

// DB connection pool
const pool = mysql.createPool({
  host: "localhost",
  user: "root",
  password: "yourpassword",
  database: "testdb"
});

// Save notification
async function saveNotification(userId, message) {
  const query = "INSERT INTO notifications (user_id, message) VALUES (?, ?)";
  const [result] = await pool.execute(query, [userId, message]);
  return result.insertId;
}

// Route
app.post("/notify", async (req, res) => {
  const { userId, message } = req.body;
  if (!userId || !message) {
    return res.status(400).json({ error: "Missing fields" });
  }
  const id = await saveNotification(userId, message);
  res.json({ success: true, id });
});

export default app;