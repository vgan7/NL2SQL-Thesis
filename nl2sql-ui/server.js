import express from "express";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const historyFilePath = path.join(__dirname, "public", "history.json");
const isProduction =
  process.env.NODE_ENV === "production" || process.argv.includes("--production");
const port = Number(process.env.PORT) || 5173;
const smartApiUrl = process.env.SMART_API_URL || "http://127.0.0.1:8000/ask";
const smartApiExportUrl = smartApiUrl.replace(/\/ask\/?$/, "/export");

async function readHistory() {
  const raw = await readFile(historyFilePath, "utf8");
  return JSON.parse(raw);
}

async function writeHistory(history) {
  await writeFile(historyFilePath, `${JSON.stringify(history, null, 2)}\n`, "utf8");
}

const app = express();
app.use(express.json());

app.post("/api/ask", async (req, res) => {
  const { question, history } = req.body ?? {};

  if (typeof question !== "string" || !question.trim()) {
    res.status(400).json({ error: "A non-empty question is required." });
    return;
  }

  const forwardedHistory = Array.isArray(history) ? history : undefined;

  try {
    const response = await fetch(smartApiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        question: question.trim(),
        ...(forwardedHistory ? { history: forwardedHistory } : {}),
      }),
    });

    const payload = await response.json().catch(() => null);

    if (!response.ok) {
      res.status(response.status).json(
        payload ?? {
          detail: `Smart API request failed with status ${response.status}.`,
        }
      );
      return;
    }

    res.json(payload);
  } catch (error) {
    res.status(502).json({
      detail:
        error instanceof Error ? error.message : "Unable to reach the Smart API.",
    });
  }
});

app.post("/api/export", async (req, res) => {
  const { sql } = req.body ?? {};

  if (typeof sql !== "string" || !sql.trim()) {
    res.status(400).json({ error: "A non-empty sql is required." });
    return;
  }

  try {
    const response = await fetch(smartApiExportUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ sql: sql.trim() }),
    });

    const payload = await response.json().catch(() => null);

    if (!response.ok) {
      res.status(response.status).json(
        payload ?? {
          detail: `Smart API export failed with status ${response.status}.`,
        }
      );
      return;
    }

    res.json(payload);
  } catch (error) {
    res.status(502).json({
      detail:
        error instanceof Error ? error.message : "Unable to reach the Smart API.",
    });
  }
});

app.get("/api/history", async (_req, res) => {
  try {
    const history = await readHistory();
    res.json(history);
  } catch (error) {
    res.status(500).json({ error: "Failed to read history." });
  }
});

app.post("/api/history/messages", async (req, res) => {
  const { chatId, message } = req.body ?? {};

  if (
    typeof chatId !== "number" ||
    !message ||
    typeof message.role !== "string" ||
    typeof message.author !== "string" ||
    typeof message.time !== "string" ||
    typeof message.content !== "string"
  ) {
    res.status(400).json({ error: "Invalid message payload." });
    return;
  }

  try {
    const history = await readHistory();
    const targetChat = history.chats.find((chat) => chat.id === chatId);

    if (!targetChat) {
      res.status(404).json({ error: "Chat not found." });
      return;
    }

    targetChat.messages.push(message);
    targetChat.snippet = message.content;
    targetChat.timestamp = "Active now";
    history.activeChatId = chatId;

    await writeHistory(history);
    res.json(history);
  } catch (error) {
    res.status(500).json({ error: "Failed to persist message." });
  }
});

app.post("/api/history/chats", async (req, res) => {
  const { title, message } = req.body ?? {};

  if (
    typeof title !== "string" ||
    !title.trim() ||
    !message ||
    typeof message.role !== "string" ||
    typeof message.author !== "string" ||
    typeof message.time !== "string" ||
    typeof message.content !== "string"
  ) {
    res.status(400).json({ error: "Invalid chat payload." });
    return;
  }

  try {
    const history = await readHistory();
    const nextId =
      history.chats.reduce((maxId, chat) => Math.max(maxId, chat.id), 0) + 1;

    const newChat = {
      id: nextId,
      title: title.trim(),
      snippet: message.content,
      timestamp: "Active now",
      messages: [message],
    };

    history.chats.unshift(newChat);
    history.activeChatId = nextId;

    await writeHistory(history);
    res.json(history);
  } catch (error) {
    res.status(500).json({ error: "Failed to create chat." });
  }
});

if (isProduction) {
  app.use(express.static(path.join(__dirname, "dist")));

  app.get("*", (_req, res) => {
    res.sendFile(path.join(__dirname, "dist", "index.html"));
  });

  app.listen(port, () => {
    console.log(`App running at http://localhost:${port}`);
  });
} else {
  const vite = await createViteServer({
    server: {
      middlewareMode: true,
      hmr: {
        clientPort: 24679,
      },
    },
    appType: "spa",
  });

  app.use(vite.middlewares);

  app.listen(port, () => {
    console.log(`Dev server running at http://localhost:${port}`);
  });
}
