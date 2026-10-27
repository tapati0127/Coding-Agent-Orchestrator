> ## Documentation Index
> Fetch the complete documentation index at: https://docs.langchain.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Quickstart

This quickstart demonstrates how to build a calculator agent using the LangGraph Graph API or the Functional API.

Build the LangGraph calculator quickstart

Build a LangGraph calculator agent in this working directory by following the LangGraph quickstart.

## Step 1: Read the guide

Detect whether this project uses Python or TypeScript/JavaScript. Fetch and follow the matching page; treat it as the source of truth for package names, model strings, and code:

* Python: [https://docs.langchain.com/oss/python/langgraph/quickstart.md](https://docs.langchain.com/oss/python/langgraph/quickstart.md)
* TypeScript: [https://docs.langchain.com/oss/javascript/langgraph/quickstart.md](https://docs.langchain.com/oss/javascript/langgraph/quickstart.md)

Ask the user whether to use the Graph API or the Functional API. If they have no preference, use the Graph API path.

## Step 2: Install dependencies

Install the packages required by the chosen path with the package manager already used in this project.

## Step 3: Configure model credentials

This quickstart uses Anthropic by default. Check whether `ANTHROPIC_API_KEY` is set. If it is not, ask the user to create a key and set it in the shell or a `.env` file, then stop and wait for confirmation. Do not invent, hardcode, or commit API keys. If the user prefers another chat model provider from the integrations docs, adapt the example accordingly after confirming.

## Step 4: Implement the calculator agent

Implement the selected quickstart path end to end: tools for add, multiply, and divide; the agent graph or functional workflow; and a sample invocation that exercises tool calling. Print the final result so the user can verify the run.

## Rules

* Stay scoped to this quickstart. Do not add deployment, evals, or unrelated frameworks unless the user asks.
* Prefer the APIs and structure shown on the fetched guide over inventing a different agent pattern.
* Ask rather than guess when a secret, API choice, or project convention is unclear.

> [!TIP]
>
> **Using an AI coding assistant?**
>
> * Install the [LangChain Docs MCP servers](https://docs.langchain.com/use-these-docs.md) to give your agent access to up-to-date LangChain documentation and examples.
>
>   Connect LangChain docs MCP servers
>
>   Connect both LangChain documentation MCP servers to my coding agent so it can look up current LangChain, LangGraph, and LangSmith docs and API reference.
>
>   Servers to add:
>
>   * `docs-langchain`: [https://docs.langchain.com/mcp](https://docs.langchain.com/mcp)
>   * `reference-langchain`: [https://reference.langchain.com/mcp](https://reference.langchain.com/mcp)
>
>   Detect which agent or editor I am using (Claude Code, Cursor, Codex CLI, Claude Desktop, Deep Agents Code, VS Code, Antigravity, or another MCP-compatible client). Use the matching setup from [https://docs.langchain.com/use-these-docs.md](https://docs.langchain.com/use-these-docs.md):
>
>   * Claude Code: `claude mcp add --transport http` for each server (project scope by default; use `--scope user` only if I ask for global access).
>   * Codex CLI: `codex mcp add` with each server URL.
>   * Cursor, Deep Agents Code, VS Code, or Antigravity: merge both entries into the MCP settings JSON using the field names shown on that page for my client.
>   * Claude Desktop: add both URLs under Settings > Connectors.
>
>   Do not invent alternate MCP URLs. After configuring, confirm both servers are listed and reachable.
> * Install [LangChain Skills](https://github.com/langchain-ai/langchain-skills) to improve your agent's performance on LangChain ecosystem tasks.
>
>   Install LangChain Skills
>
>   Install LangChain Skills for my coding agent so it can perform better on LangChain, LangGraph, and Deep Agents tasks.
>
>   Use the Agent Skills installer from [https://github.com/langchain-ai/langchain-skills](https://github.com/langchain-ai/langchain-skills):
>
>   ```bash theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
>   npx skills add langchain-ai/langchain-skills --skill '*' --yes
>   ```
>
>   If I ask for a global install instead, use:
>
>   ```bash theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
>   npx skills add langchain-ai/langchain-skills --skill '*' --yes --global
>   ```
>
>   Detect which agent or editor I am using. If I use Claude Code and prefer the plugin path, follow the marketplace install from that repository README (`/plugin marketplace add` then `/plugin install`). Do not invent alternate skill package names or install URLs. After installing, confirm the skills are available to the agent.

* [Use the Graph API](#use-the-graph-api) if you prefer to define your agent as a graph of nodes and edges.
* [Use the Functional API](#use-the-functional-api) if you prefer to define your agent as a single function.

For conceptual information, see [Graph API overview](https://docs.langchain.com/oss/javascript/langgraph/graph-api.md) and [Functional API overview](https://docs.langchain.com/oss/javascript/langgraph/functional-api.md).

> [!NOTE]
>
> For this example, you will need to set up a [Claude (Anthropic)](https://www.anthropic.com/) account and get an API key. Then, set the `ANTHROPIC_API_KEY` environment variable in your terminal. See [chat model integrations](https://docs.langchain.com/oss/javascript/integrations/chat.md) for all available providers. If you use [LangSmith Gateway](https://docs.langchain.com/langsmith/llm-gateway.md), you can [bring your own provider keys](https://docs.langchain.com/langsmith/llm-gateway-quickstart.md#send-a-request) or use [Gateway Credits](https://docs.langchain.com/langsmith/llm-gateway-credits.md) to access models without a provider key.

**Use the Graph API**

## 1. Define tools and model

In this example, we'll use the Claude Sonnet 4.5 model and define tools for addition, multiplication, and division.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import { ChatAnthropic } from "@langchain/anthropic";
import { tool } from "@langchain/core/tools";
import * as z from "zod";

const model = new ChatAnthropic({
  model: "claude-sonnet-4-6",
  temperature: 0,
});

// Define tools
const add = tool(({ a, b }) => a + b, {
  name: "add",
  description: "Add two numbers",
  schema: z.object({
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  }),
});

const multiply = tool(({ a, b }) => a * b, {
  name: "multiply",
  description: "Multiply two numbers",
  schema: z.object({
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  }),
});

const divide = tool(({ a, b }) => a / b, {
  name: "divide",
  description: "Divide two numbers",
  schema: z.object({
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  }),
});

// Augment the LLM with tools
const toolsByName = {
  [add.name]: add,
  [multiply.name]: multiply,
  [divide.name]: divide,
};
const tools = Object.values(toolsByName);
const modelWithTools = model.bindTools(tools);
```

## 2. Define state

The graph's state is used to store the messages and the number of LLM calls.

> [!TIP]
>
> State in LangGraph persists throughout the agent's execution.
>
> The `MessagesValue` provides a built-in reducer for appending messages. The `llmCalls` field uses a `ReducedValue` with `(x, y) => x + y` to accumulate the count.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import {
  StateGraph,
  StateSchema,
  MessagesValue,
  ReducedValue,
  GraphNode,
  ConditionalEdgeRouter,
  START,
  END,
} from "@langchain/langgraph";
import { z } from "zod/v4";

const MessagesState = new StateSchema({
  messages: MessagesValue,
  llmCalls: new ReducedValue(
    z.number().default(0),
    { reducer: (x, y) => x + y }
  ),
});
```

## 3. Define model node

The model node is used to call the LLM and decide whether to call a tool or not.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import { SystemMessage } from "@langchain/core/messages";

const llmCall: GraphNode<typeof MessagesState> = async (state) => {
  const response = await modelWithTools.invoke([
    new SystemMessage(
      "You are a helpful assistant tasked with performing arithmetic on a set of inputs."
    ),
    ...state.messages,
  ]);
  return {
    messages: [response],
    llmCalls: 1,
  };
};
```

## 4. Define tool node

The tool node is used to call the tools and return the results.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import { AIMessage, ToolMessage } from "@langchain/core/messages";

const toolNode: GraphNode<typeof MessagesState> = async (state) => {
  const lastMessage = state.messages.at(-1);

  if (lastMessage == null || !AIMessage.isInstance(lastMessage)) {
    return { messages: [] };
  }

  const result: ToolMessage[] = [];
  for (const toolCall of lastMessage.tool_calls ?? []) {
    const tool = toolsByName[toolCall.name];
    const observation = await tool.invoke(toolCall);
    result.push(observation);
  }

  return { messages: result };
};
```

## 5. Define end logic

The conditional edge function is used to route to the tool node or end based upon whether the LLM made a tool call.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
const shouldContinue: ConditionalEdgeRouter<{ InputSchema: typeof MessagesState; Nodes: "toolNode" }> = (state) => {
  const lastMessage = state.messages.at(-1);

  // Check if it's an AIMessage before accessing tool_calls
  if (!lastMessage || !AIMessage.isInstance(lastMessage)) {
    return END;
  }

  // If the LLM makes a tool call, then perform an action
  if (lastMessage.tool_calls?.length) {
    return "toolNode";
  }

  // Otherwise, we stop (reply to the user)
  return END;
};
```

## 6. Build and compile the agent

The agent is built using the [`StateGraph`](https://reference.langchain.com/javascript/langchain-langgraph/index/StateGraph) class and compiled using the [`compile`](https://reference.langchain.com/javascript/langchain-langgraph/index/StateGraph/compile) method.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
const agent = new StateGraph(MessagesState)
  .addNode("llmCall", llmCall)
  .addNode("toolNode", toolNode)
  .addEdge(START, "llmCall")
  .addConditionalEdges("llmCall", shouldContinue, ["toolNode", END])
  .addEdge("toolNode", "llmCall")
  .compile();

// Invoke
import { HumanMessage } from "@langchain/core/messages";
const result = await agent.invoke({
  messages: [new HumanMessage("Add 3 and 4.")],
});

for (const message of result.messages) {
  console.log(`[${message.type}]: ${message.text}`);
}
```

> [!TIP]
>
> Trace and debug your agent with [LangSmith](https://smith.langchain.com?utm_source=docs\&utm_medium=cta\&utm_campaign=langsmith-signup\&utm_content=oss-langgraph-quickstart). Follow the [tracing quickstart](https://docs.langchain.com/langsmith/trace-with-langgraph.md) to get set up. When ready for production, see [Deploy](https://docs.langchain.com/langsmith/deployment.md) for hosting options.
>
> We recommend you also set up [LangSmith Engine](https://docs.langchain.com/langsmith/engine.md) which monitors your traces, detects issues, and proposes fixes.

Congratulations! You've built your first agent using the LangGraph Graph API.

> **Full code example**
>
> ```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
> // Step 1: Define tools and model
>
> import { ChatAnthropic } from "@langchain/anthropic";
> import { tool } from "@langchain/core/tools";
> import * as z from "zod";
>
> const model = new ChatAnthropic({
>   model: "claude-sonnet-4-6",
>   temperature: 0,
> });
>
> // Define tools
> const add = tool(({ a, b }) => a + b, {
>   name: "add",
>   description: "Add two numbers",
>   schema: z.object({
>     a: z.number().describe("First number"),
>     b: z.number().describe("Second number"),
>   }),
> });
>
> const multiply = tool(({ a, b }) => a * b, {
>   name: "multiply",
>   description: "Multiply two numbers",
>   schema: z.object({
>     a: z.number().describe("First number"),
>     b: z.number().describe("Second number"),
>   }),
> });
>
> const divide = tool(({ a, b }) => a / b, {
>   name: "divide",
>   description: "Divide two numbers",
>   schema: z.object({
>     a: z.number().describe("First number"),
>     b: z.number().describe("Second number"),
>   }),
> });
>
> // Augment the LLM with tools
> const toolsByName = {
>   [add.name]: add,
>   [multiply.name]: multiply,
>   [divide.name]: divide,
> };
> const tools = Object.values(toolsByName);
> const modelWithTools = model.bindTools(tools);
> ```
>
> ```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
> // Step 2: Define state
>
> import {
>   StateGraph,
>   StateSchema,
>   MessagesValue,
>   ReducedValue,
>   GraphNode,
>   ConditionalEdgeRouter,
>   START,
>   END,
> } from "@langchain/langgraph";
> import * as z from "zod";
>
> const MessagesState = new StateSchema({
>   messages: MessagesValue,
>   llmCalls: new ReducedValue(
>     z.number().default(0),
>     { reducer: (x, y) => x + y }
>   ),
> });
> ```
>
> ```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
> // Step 3: Define model node
>
> import { SystemMessage, AIMessage, ToolMessage } from "@langchain/core/messages";
>
> const llmCall: GraphNode<typeof MessagesState> = async (state) => {
>   return {
>     messages: [await modelWithTools.invoke([
>       new SystemMessage(
>         "You are a helpful assistant tasked with performing arithmetic on a set of inputs."
>       ),
>       ...state.messages,
>     ])],
>     llmCalls: 1,
>   };
> };
>
> // Step 4: Define tool node
>
> const toolNode: GraphNode<typeof MessagesState> = async (state) => {
>   const lastMessage = state.messages.at(-1);
>
>   if (lastMessage == null || !AIMessage.isInstance(lastMessage)) {
>     return { messages: [] };
>   }
>
>   const result: ToolMessage[] = [];
>   for (const toolCall of lastMessage.tool_calls ?? []) {
>     const tool = toolsByName[toolCall.name];
>     const observation = await tool.invoke(toolCall);
>     result.push(observation);
>   }
>
>   return { messages: result };
> };
> ```
>
> ```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
> // Step 5: Define logic to determine whether to end
> import { ConditionalEdgeRouter, END } from "@langchain/langgraph";
>
> const shouldContinue: ConditionalEdgeRouter<{ InputSchema: typeof MessagesState; Nodes: "toolNode" }> = (state) => {
>   const lastMessage = state.messages.at(-1);
>
>   // Check if it's an AIMessage before accessing tool_calls
>   if (!lastMessage || !AIMessage.isInstance(lastMessage)) {
>     return END;
>   }
>
>   // If the LLM makes a tool call, then perform an action
>   if (lastMessage.tool_calls?.length) {
>     return "toolNode";
>   }
>
>   // Otherwise, we stop (reply to the user)
>   return END;
> };
> ```
>
> ```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
> // Step 6: Build and compile the agent
> import { HumanMessage } from "@langchain/core/messages";
> import { StateGraph, START, END } from "@langchain/langgraph";
>
> const agent = new StateGraph(MessagesState)
>   .addNode("llmCall", llmCall)
>   .addNode("toolNode", toolNode)
>   .addEdge(START, "llmCall")
>   .addConditionalEdges("llmCall", shouldContinue, ["toolNode", END])
>   .addEdge("toolNode", "llmCall")
>   .compile();
>
> // Invoke
> const result = await agent.invoke({
>   messages: [new HumanMessage("Add 3 and 4.")],
> });
>
> for (const message of result.messages) {
>   console.log(`[${message.type}]: ${message.text}`);
> }
> ```

**Use the Functional API**

## 1. Define tools and model

In this example, we'll use the Claude Sonnet 4.5 model and define tools for addition, multiplication, and division.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import { ChatAnthropic } from "@langchain/anthropic";
import { tool } from "@langchain/core/tools";
import * as z from "zod";

const model = new ChatAnthropic({
  model: "claude-sonnet-4-6",
  temperature: 0,
});

// Define tools
const add = tool(({ a, b }) => a + b, {
  name: "add",
  description: "Add two numbers",
  schema: z.object({
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  }),
});

const multiply = tool(({ a, b }) => a * b, {
  name: "multiply",
  description: "Multiply two numbers",
  schema: z.object({
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  }),
});

const divide = tool(({ a, b }) => a / b, {
  name: "divide",
  description: "Divide two numbers",
  schema: z.object({
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  }),
});

// Augment the LLM with tools
const toolsByName = {
  [add.name]: add,
  [multiply.name]: multiply,
  [divide.name]: divide,
};
const tools = Object.values(toolsByName);
const modelWithTools = model.bindTools(tools);

```

## 2. Define model node

The model node is used to call the LLM and decide whether to call a tool or not.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import { task, entrypoint } from "@langchain/langgraph";
import { SystemMessage } from "@langchain/core/messages";

const callLlm = task({ name: "callLlm" }, async (messages: BaseMessage[]) => {
  return modelWithTools.invoke([
    new SystemMessage(
      "You are a helpful assistant tasked with performing arithmetic on a set of inputs."
    ),
    ...messages,
  ]);
});
```

## 3. Define tool node

The tool node is used to call the tools and return the results.

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import type { ToolCall } from "@langchain/core/messages/tool";

const callTool = task({ name: "callTool" }, async (toolCall: ToolCall) => {
  const tool = toolsByName[toolCall.name];
  return tool.invoke(toolCall);
});
```

## 4. Define agent

```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
import { addMessages } from "@langchain/langgraph";
import { type BaseMessage } from "@langchain/core/messages";

const agent = entrypoint({ name: "agent" }, async (messages: BaseMessage[]) => {
  let modelResponse = await callLlm(messages);

  while (true) {
    if (!modelResponse.tool_calls?.length) {
      break;
    }

    // Execute tools
    const toolResults = await Promise.all(
      modelResponse.tool_calls.map((toolCall) => callTool(toolCall))
    );
    messages = addMessages(messages, [modelResponse, ...toolResults]);
    modelResponse = await callLlm(messages);
  }

  return messages;
});

// Invoke
import { HumanMessage } from "@langchain/core/messages";

const result = await agent.invoke([new HumanMessage("Add 3 and 4.")]);

for (const message of result) {
  console.log(`[${message.getType()}]: ${message.text}`);
}
```

> [!TIP]
>
> Trace and debug your agent with [LangSmith](https://smith.langchain.com?utm_source=docs\&utm_medium=cta\&utm_campaign=langsmith-signup\&utm_content=oss-langgraph-quickstart). Follow the [tracing quickstart](https://docs.langchain.com/langsmith/trace-with-langgraph.md) to get set up. When ready for production, see [Deploy](https://docs.langchain.com/langsmith/deployment.md) for hosting options.
>
> We recommend you also set up [LangSmith Engine](https://docs.langchain.com/langsmith/engine.md) which monitors your traces, detects issues, and proposes fixes.

Congratulations! You've built your first agent using the LangGraph Functional API.

> **Full code example**
>
> ```typescript theme={"theme":{"light":"catppuccin-latte","dark":"catppuccin-mocha"}}
> import { ChatAnthropic } from "@langchain/anthropic";
> import { tool } from "@langchain/core/tools";
> import {
>   task,
>   entrypoint,
>   addMessages,
> } from "@langchain/langgraph";
> import {
>   SystemMessage,
>   HumanMessage,
>   type BaseMessage,
> } from "@langchain/core/messages";
> import type { ToolCall } from "@langchain/core/messages/tool";
> import * as z from "zod";
>
> // Step 1: Define tools and model
>
> const model = new ChatAnthropic({
>   model: "claude-sonnet-4-6",
>   temperature: 0,
> });
>
> // Define tools
> const add = tool(({ a, b }) => a + b, {
>   name: "add",
>   description: "Add two numbers",
>   schema: z.object({
>     a: z.number().describe("First number"),
>     b: z.number().describe("Second number"),
>   }),
> });
>
> const multiply = tool(({ a, b }) => a * b, {
>   name: "multiply",
>   description: "Multiply two numbers",
>   schema: z.object({
>     a: z.number().describe("First number"),
>     b: z.number().describe("Second number"),
>   }),
> });
>
> const divide = tool(({ a, b }) => a / b, {
>   name: "divide",
>   description: "Divide two numbers",
>   schema: z.object({
>     a: z.number().describe("First number"),
>     b: z.number().describe("Second number"),
>   }),
> });
>
> // Augment the LLM with tools
> const toolsByName = {
>   [add.name]: add,
>   [multiply.name]: multiply,
>   [divide.name]: divide,
> };
> const tools = Object.values(toolsByName);
> const modelWithTools = model.bindTools(tools);
>
> // Step 2: Define model node
>
> const callLlm = task({ name: "callLlm" }, async (messages: BaseMessage[]) => {
>   return modelWithTools.invoke([
>     new SystemMessage(
>       "You are a helpful assistant tasked with performing arithmetic on a set of inputs."
>     ),
>     ...messages,
>   ]);
> });
>
> // Step 3: Define tool node
>
> const callTool = task({ name: "callTool" }, async (toolCall: ToolCall) => {
>   const tool = toolsByName[toolCall.name];
>   return tool.invoke(toolCall);
> });
>
> // Step 4: Define agent
>
> const agent = entrypoint({ name: "agent" }, async (messages: BaseMessage[]) => {
>   let modelResponse = await callLlm(messages);
>
>   while (true) {
>     if (!modelResponse.tool_calls?.length) {
>       break;
>     }
>
>     // Execute tools
>     const toolResults = await Promise.all(
>       modelResponse.tool_calls.map((toolCall) => callTool(toolCall))
>     );
>     messages = addMessages(messages, [modelResponse, ...toolResults]);
>     modelResponse = await callLlm(messages);
>   }
>
>   return messages;
> });
>
> // Invoke
>
> const result = await agent.invoke([new HumanMessage("Add 3 and 4.")]);
>
> for (const message of result) {
>   console.log(`[${message.type}]: ${message.text}`);
> }
> ```

***

[Connect these docs](https://docs.langchain.com/use-these-docs.md) to your agent of choice via MCP for real-time answers.

[Edit this page on GitHub](https://github.com/langchain-ai/docs/edit/main/src/oss/langgraph/quickstart.mdx) or [file an issue](https://github.com/langchain-ai/docs/issues/new/choose).
