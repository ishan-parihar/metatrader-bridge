//+------------------------------------------------------------------+
//|                                                BridgeConnectorEA |
//|                             MT5 ↔ MCP EA bridge (heartbeat)       |
//+------------------------------------------------------------------+
#property copyright "MetaTraderBridge"
#property version   "2.51"

// Trading includes
#include <Trade\Trade.mqh>
#include "TrailingStopManager.mqh"
#include "BracketManager.mqh"
#include "PositionTimeManager.mqh"

// Inputs
input string GatewayBaseURL = "http://127.0.0.1:8020";
input string GatewayURL = "http://127.0.0.1:8020/bridge/terminal/heartbeat"; // deprecated, use GatewayBaseURL
input int    HeartbeatSeconds = 1;        // Heartbeat interval (seconds between heartbeats)
input int    CommandPollIntervalMs = 100; // Milliseconds between command polls (HTTP fallback)
input int    MaxCommandsPerTick = 20;     // Max commands to process per timer tick
input int    TcpPollMs = 1;               // TCP command polling interval in ms (lower = faster, min 1)
input bool   EnableDebugLogs = false;

// TCP Bridge inputs
input string TCPBridgeHost = "127.0.0.1";
input int    TCPBridgePort = 8025;
input bool   EnableTCPBridge = true;  // Set false to use HTTP polling fallback

// Trailing stop inputs
input long   TrailingMagicFilter = 0;  // 0 = trail all, >0 = only trail positions with this magic number

// Bracket order inputs
input long   BracketMagicFilter = 0;   // 0 = manage all brackets, >0 = only manage brackets with this magic number

// Internal state
int g_last_status = 0;
int g_socket = INVALID_HANDLE;
bool g_tcp_connected = false;
int g_tcp_send_failures = 0;      // Consecutive send failure counter
const int MAX_TCP_SEND_FAILURES = 3; // Mark connection dead after N consecutive failures
int g_heartbeat_counter = 0;       // Ticks since last heartbeat
int g_heartbeat_interval = 0;      // Ticks between heartbeats (computed from TcpPollMs)

// Global trailing stop manager
CTrailingStopManager g_trailing_manager;

// Global bracket order manager
CBracketManager g_bracket_manager;

// Global position time manager
CPositionTimeManager g_position_time_manager;

// Custom indicator handle cache (generic iCustom wrapper)
int g_custom_handles[32];
string g_custom_keys[32];
int g_custom_count = 0;

void DebugLog(const string message)
{
   if(EnableDebugLogs)
      Print(message);
}

string JsonEscape(const string s)
{
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
}

// Inline ErrorDescription (stdlib.mqh not always available)
string ErrorDescriptionLocal(int error_code)
{
   switch(error_code)
   {
      case 0:     return "No error";
      case 1:     return "No error returned, the operation was successful";
      case 2:     return "Common error";
      case 3:     return "Invalid parameters";
      case 4:     return "Array index out of range";
      case 5:     return "No memory for function call";
      case 6:     return "Invalid function parameters count";
      case 7:     return "Invalid function parameter value";
      case 8:     return "Internal error";
      case 9:     return "Not enough memory for internal MQL5-RTL";
      case 10:    return "Invalid function call";
      case 11:    return "Not enough memory";
      case 4001:  return "Internal error";
      case 4002:  return "Wrong file name";
      case 4003:  return "Too long file name";
      case 4004:  return "Cannot open file";
      case 4009:  return "No file";
      case 4014:  return "Invalid handle";
      case 4050:  return "Invalid function parameters count";
      case 4051:  return "Invalid function parameter value";
      case 4052:  return "String function internal error";
      case 4053:  return "Array index out of range";
      case 4054:  return "No memory for an array";
      case 4055:  return "Not enough memory for string";
      case 4056:  return "Not enough memory";
      case 4057:  return "Incorrect pointer";
      case 4058:  return "Pointer cannot be dereferenced";
      case 4062:  return "Access violation";
      case 4064:  return "Null pointer";
      case 4065:  return "Array of zero length";
      case 4066:  return "Requested data not found";
      case 4067:  return "Requested object not found";
      case 4099:  return "End of file";
      case 4100:  return "Some file error";
      case 4103:  return "Object already exists";
      case 4104:  return "Unknown object";
      case 4105:  return "Not enough memory for object";
      case 4106:  return "Unknown object property";
      case 4107:  return "Object does not support property";
      case 4108:  return "Read only property";
      case 4109:  return "Invalid pointer";
      case 4110:  return "Null pointer";
      case 4111:  return "Unsupported operation";
      case 4200:  return "Object is not found";
      case 4201:  return "Invalid object type";
      case 4202:  return "Unknown symbol";
      case 4203:  return "Invalid price";
      case 4204:  return "Invalid stops";
      case 4205:  return "Invalid trade volume";
      case 4206:  return "Market is closed";
      case 4207:  return "Trade is disabled";
      case 4208:  return "Not enough money";
      case 4209:  return "Too many orders";
      case 4750:  return "Connection lost";
      case 4751:  return "Timeout";
      case 4752:  return "Invalid request";
      case 4753:  return "Invalid client";
      case 4754:  return "No connection";
      case 4755:  return "Timeout";
      case 4756:  return "Unknown request";
      case 4757:  return "Invalid version";
      case 4758:  return "Unauthorized";
      case 4759:  return "Too many requests";
      case 4760:  return "Forbidden";
      case 4761:  return "Unknown command";
      case 4762:  return "Not implemented";
      case 4763:  return "Internal error";
      case 4764:  return "Server not found";
      case 4765:  return "Unknown host";
      case 4766:  return "Connection refused";
      case 4767:  return "Connection reset";
      case 4768:  return "Connection timeout";
      case 4769:  return "Connection error";
      default:    return "Unknown error #" + IntegerToString(error_code);
   }
}

string BuildHeartbeatJson()
{
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   int build = (int)TerminalInfoInteger(TERMINAL_BUILD);
   datetime now = TimeCurrent();
   string payload = StringFormat(
      "{\"server\":\"%s\",\"build\":%d,\"account_id\":\"%I64d\",\"login\":%I64d,\"timestamp\":\"%s\"}",
      JsonEscape(server), build, login, login, TimeToString(now, TIME_DATE|TIME_SECONDS)
   );
   return payload;
}

// Convert a flat JSON object to KV format for existing ParseKV()
// e.g. {"type":"get_bars","request_id":"abc","count":100} → "type=get_bars&request_id=abc&count=100"
string JsonToKV(const string json)
{
   string result = "";
   int pos = 0;
   int len = StringLen(json);
   
   while(pos < len)
   {
      // Find next key (opening quote after { or ,)
      int key_start = StringFind(json, "\"", pos);
      if(key_start < 0) break;
      key_start++;
      int key_end = StringFind(json, "\"", key_start);
      if(key_end < 0) break;
      string key = StringSubstr(json, key_start, key_end - key_start);
      
      // Find colon after key
      int colon = StringFind(json, ":", key_end);
      if(colon < 0) break;
      colon++;
      
      // Skip whitespace
      while(colon < len)
      {
         ushort c = StringGetCharacter(json, colon);
         if(c > 32) break;
         colon++;
      }
      
      string value = "";
      if(colon >= len) break;
      
      if(StringGetCharacter(json, colon) == '\"')
      {
         // String value - find closing quote (handle escapes)
         int val_start = colon + 1;
         int val_end = val_start;
         while(val_end < len)
         {
            ushort c = StringGetCharacter(json, val_end);
            if(c == '\\' && val_end + 1 < len)
            {
               val_end += 2;
               continue;
            }
            if(c == '\"') break;
            val_end++;
         }
         value = StringSubstr(json, val_start, val_end - val_start);
         pos = val_end + 1;
      }
      else
      {
         // Numeric/boolean/null value
         int val_start = colon;
         int val_end = val_start;
         while(val_end < len)
         {
            ushort c = StringGetCharacter(json, val_end);
            if(c == ',' || c == '}' || c == ']' || c <= 32) break;
            val_end++;
         }
         value = StringSubstr(json, val_start, val_end - val_start);
         pos = val_end;
      }
      
      if(key != "" && value != "")
      {
         if(result != "") result += "&";
         result += key + "=" + value;
      }
   }
   return result;
}

// Send a length-prefixed JSON frame over TCP socket
// Format: [4 bytes big-endian uint32 length][N bytes JSON payload]
bool SocketSendFrame(const string json)
{
   if(g_socket == INVALID_HANDLE || !g_tcp_connected)
      return false;
   
   // Validate actual socket state via MQL5 API
   if(!SocketIsConnected(g_socket))
   {
      g_tcp_connected = false;
      return false;
   }
   
   // Convert JSON string to uchar array (UTF-8)
   uchar data[];
   int data_len = StringToCharArray(json, data, 0, StringLen(json), CP_UTF8);
   
   // Build frame: 4-byte big-endian length + payload
   uchar frame[];
   int frame_len = 4 + data_len;
   ArrayResize(frame, frame_len);
   
   // Big-endian uint32 length
   frame[0] = (uchar)((data_len >> 24) & 0xFF);
   frame[1] = (uchar)((data_len >> 16) & 0xFF);
   frame[2] = (uchar)((data_len >> 8) & 0xFF);
   frame[3] = (uchar)(data_len & 0xFF);
   
   // Copy payload
   for(int i = 0; i < data_len; i++)
      frame[4 + i] = data[i];
   
   ResetLastError();
   int sent = SocketSend(g_socket, frame, frame_len);
   
   // Wine/MQL5 bug: SocketSend ALWAYS returns -1 (error 5273) even when data
   // is successfully transmitted. The bridge confirms it receives all frames.
   // Track consecutive failures: if SocketRead later confirms disconnect, we
   // know the connection was dead. If reads succeed, sends were fine despite
   // the error code.
   if(sent != frame_len)
   {
      int err = GetLastError();
      if(err != 5273)
      {
         g_tcp_send_failures++;
         if(EnableDebugLogs)
            Print("SocketSendFrame: non-Wine send error (sent=", sent, "/", frame_len, " err=", err, " failures=", g_tcp_send_failures, ")");
         if(g_tcp_send_failures >= MAX_TCP_SEND_FAILURES)
         {
            Print("SocketSendFrame: ", MAX_TCP_SEND_FAILURES, " consecutive non-Wine send failures, marking dead");
            g_tcp_connected = false;
            return false;
         }
      }
      // Error 5273 is the Wine bug — not counted as failure
   }
   else
   {
      // Successful send resets counter
      g_tcp_send_failures = 0;
   }
   
   return true;
}

// Receive a length-prefixed JSON frame from TCP socket
// Returns true on success, false on no data available
//
// CRITICAL: MQL5 SocketRead returns error 5273 if you request more bytes than
// are currently available in the OS socket buffer. The community-validated
// pattern (Rene Balke, MQL5 Forum #337430, Jan 2025) is to always read exactly
// what SocketIsReadable() reports.
//
// Partial payload reads are accumulated — we never discard already-read bytes.
bool SocketReceiveFrame(string &json_out)
{
   // Validate socket liveness via MQL5 API, not just our software flag
   if(g_socket == INVALID_HANDLE || !g_tcp_connected)
      return false;
   if(!SocketIsConnected(g_socket))
   {
      g_tcp_connected = false;
      Print("SocketReceiveFrame: SocketIsConnected=false, marking dead");
      return false;
   }
   
   // Phase 1: Read the 4-byte length header
   // Read EXACTLY what SocketIsReadable reports, capped at 4 bytes for header
   uint available = SocketIsReadable(g_socket);
   if(available == 0)
      return false; // No data — normal, connection stays alive
   
   uchar header[];
   ArrayResize(header, 4);
   
   int header_read = 0;
   while(header_read < 4)
   {
      ResetLastError();
      available = SocketIsReadable(g_socket);
      if(available == 0)
      {
         // Header partially arrived — wait for remainder
         Sleep(10);
         available = SocketIsReadable(g_socket);
         if(available == 0 && header_read > 0)
         {
            // Had partial header but no more data — corrupt frame
            Print("SocketReceiveFrame: partial header (", header_read, "/4 bytes), connection lost");
            g_tcp_connected = false;
            return false;
         }
         else if(available == 0)
            return false; // Nothing arrived yet — normal
      }
      
      // Read exactly min(remaining header bytes, available bytes)
      int remaining = 4 - header_read;
      int to_read = (int)MathMin(available, (uint)remaining);
      
      uchar chunk[];
      ArrayResize(chunk, to_read);
      int read = SocketRead(g_socket, chunk, to_read, 100);
      if(read <= 0)
      {
         int err = GetLastError();
         g_tcp_connected = false;
         Print("SocketReceiveFrame: header chunk read failed (chunk=", read, ", err=", err, ")");
         return false;
      }
      
      // Append chunk to header buffer
      for(int i = 0; i < read; i++)
         header[header_read + i] = chunk[i];
      header_read += read;
   }
   
   // Parse big-endian uint32 length
   uint payload_len = ((uint)header[0] << 24) | ((uint)header[1] << 16) | ((uint)header[2] << 8) | (uint)header[3];
   
   if(payload_len == 0 || payload_len > 10000000)
   {
      Print("SocketReceiveFrame: invalid payload length ", payload_len);
      return false;
   }
   
   // Phase 2: Read the payload — accumulate bytes until we have all of them
   // NEVER request more than SocketIsReadable() reports
   uchar payload[];
   ArrayResize(payload, (int)payload_len);
   
   int total_read = 0;
   int read_attempts = 0;
   const int MAX_READ_ATTEMPTS = 200; // ~2 seconds at 10ms intervals
   
   while(total_read < (int)payload_len)
   {
      ResetLastError();
      available = SocketIsReadable(g_socket);
      
      if(available == 0)
      {
         read_attempts++;
         if(read_attempts > MAX_READ_ATTEMPTS)
         {
            // Connection is alive but server stopped sending — timeout
            Print("SocketReceiveFrame: read timeout at ", total_read, "/", payload_len);
            return false;
         }
         Sleep(10);
         continue;
      }
      
      // Read exactly min(remaining payload bytes, available bytes)
      int remaining = (int)payload_len - total_read;
      int to_read = (int)MathMin(available, (uint)remaining);
      
      uchar chunk[];
      ArrayResize(chunk, to_read);
      int read = SocketRead(g_socket, chunk, to_read, 100);
      if(read <= 0)
      {
         int err = GetLastError();
         g_tcp_connected = false;
         Print("SocketReceiveFrame: payload chunk read failed at ", total_read, "/", payload_len, " (read=", read, ", err=", err, ")");
         return false;
      }
      
      // Append chunk to payload buffer
      for(int i = 0; i < read; i++)
         payload[total_read + i] = chunk[i];
      total_read += read;
      read_attempts = 0; // Reset counter on successful read
   }
   
   // Convert payload to string (UTF-8)
   json_out = CharArrayToString(payload, 0, (int)payload_len, CP_UTF8);
   return true;
}

int HttpPost(const string url, const string json, string &resp_out)
{
   // Ensure JSON content-type so FastAPI parses body
   string headers = "Content-Type: application/json\r\n";
   char data[];
   StringToCharArray(json, data, 0, StringLen(json), CP_UTF8);
   char result[];
   string response_headers;
   ResetLastError();
   int code = WebRequest("POST", url, headers, 10000, data, result, response_headers);
   resp_out = CharArrayToString(result, 0, -1, CP_UTF8);
   return code;
}

int OnInit()
  {
   // Attempt TCP connection if enabled
     if(EnableTCPBridge)
     {
        Print("BridgeConnectorEA: Attempting TCP connection to ", TCPBridgeHost, ":", TCPBridgePort);
        
        ResetLastError();
        g_socket = SocketCreate();
        int create_err = GetLastError();
        Print("BridgeConnectorEA: SocketCreate returned handle=", g_socket, " (INVALID_HANDLE=", INVALID_HANDLE, "), error=", create_err);
        
        if(g_socket != INVALID_HANDLE)
        {
           ResetLastError();
           g_tcp_connected = SocketConnect(g_socket, TCPBridgeHost, TCPBridgePort, 3000);
           int connect_err = GetLastError();
           if(g_tcp_connected)
           {
              g_tcp_send_failures = 0; // Reset counter on initial connect
              Print("BridgeConnectorEA: TCP connected to ", TCPBridgeHost, ":", TCPBridgePort);
           }
           else
              Print("BridgeConnectorEA: TCP SocketConnect failed (error=", connect_err, ": ", ErrorDescriptionLocal(connect_err), "), falling back to HTTP polling");
        }
        else
        {
           Print("BridgeConnectorEA: SocketCreate failed (error=", create_err, ": ", ErrorDescriptionLocal(create_err), "), falling back to HTTP polling");
        }
     }
   
    Print("BridgeConnectorEA initialized (millisecond timer)");
    // Use millisecond timer for low-latency command polling
    // TcpPollMs=1 means we check for commands every 1ms (~0.5ms avg latency)
    // Heartbeat sent every N ticks to maintain ~1s interval
    int poll_ms = TcpPollMs;
    if(poll_ms < 1) poll_ms = 1;
    g_heartbeat_interval = (HeartbeatSeconds * 1000) / poll_ms;
    if(g_heartbeat_interval < 1) g_heartbeat_interval = 1;
     EventSetMillisecondTimer(poll_ms);
      g_trailing_manager.SetMagicFilter(TrailingMagicFilter);
      g_bracket_manager.SetMagicFilter(BracketMagicFilter);
      g_bracket_manager.RecoverFromOrders();

      // Recover position time exits from position comments
      ::PositionsTotal();
      int pos_total = PositionsTotal();
      for(int i = 0; i < pos_total; i++)
      {
         ulong ticket = PositionGetTicket(i);
         if(PositionSelectByTicket(ticket))
         {
            string cmt = PositionGetString(POSITION_COMMENT);
            if(StringFind(cmt, "time:") >= 0)
            {
               g_position_time_manager.RecoverFromComment(ticket, cmt);
            }
         }
      }

      return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   if(g_socket != INVALID_HANDLE)
   {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      g_tcp_connected = false;
   }
   Print("BridgeConnectorEA deinitialized: ", reason);
   EventKillTimer();
  }

void OnTimer()
{
   g_heartbeat_counter++;
   
   // Send heartbeat at configured interval
   if(g_heartbeat_counter >= g_heartbeat_interval)
   {
      g_heartbeat_counter = 0;
      SendHeartbeat();
   }
   
   // TCP connection health check (throttled to once per second)
   if(EnableTCPBridge && g_tcp_connected && g_heartbeat_counter == 0)
   {
      if(!SocketIsConnected(g_socket))
      {
         Print("OnTimer: SocketIsConnected=false, marking dead");
         g_tcp_connected = false;
         if(g_socket != INVALID_HANDLE)
         {
            SocketClose(g_socket);
            g_socket = INVALID_HANDLE;
         }
      }
   }
   
   // TCP reconnection (throttled to once per second)
   if(EnableTCPBridge && !g_tcp_connected && g_heartbeat_counter == 0)
   {
      if(g_socket != INVALID_HANDLE)
      {
         SocketClose(g_socket);
         g_socket = INVALID_HANDLE;
      }
      
      ResetLastError();
      g_socket = SocketCreate();
      int create_err = GetLastError();
      
      if(g_socket != INVALID_HANDLE)
      {
         ResetLastError();
         g_tcp_connected = SocketConnect(g_socket, TCPBridgeHost, TCPBridgePort, 3000);
         int connect_err = GetLastError();
         if(g_tcp_connected)
         {
            g_tcp_send_failures = 0;
            Print("BridgeConnectorEA: TCP reconnected to ", TCPBridgeHost, ":", TCPBridgePort);
         }
         else
         {
            if(EnableDebugLogs)
               Print("BridgeConnectorEA: TCP reconnect failed (error=", connect_err, ")");
            SocketClose(g_socket);
            g_socket = INVALID_HANDLE;
         }
      }
      else
      {
         if(EnableDebugLogs)
            Print("BridgeConnectorEA: SocketCreate failed during reconnect (error=", create_err, ")");
      }
   }
   
    // Process commands every tick — this is the low-latency path
    ProcessAllPendingCommands();
    
    // Process trailing stops
    if(g_trailing_manager.GetActiveCount() > 0)
       g_trailing_manager.ProcessAll();

    // Process position time exits
    if(g_position_time_manager.GetActiveCount() > 0)
       g_position_time_manager.CheckAll();

    // Process bracket orders
   if(g_bracket_manager.GetBracketCount() > 0)
      g_bracket_manager.ProcessAll();
}

void SendHeartbeat()
{
   string json = BuildHeartbeatJson();
   
   if(g_tcp_connected)
   {
      // Send heartbeat via TCP as a frame
      // BuildHeartbeatJson returns: {"server":"...","build":N,"account_id":"...","login":N,"timestamp":"..."}
      // Prepend "type":"heartbeat" to make: {"type":"heartbeat","server":"...","build":N,...}
      string heartbeat_frame = StringFormat("{\"type\":\"heartbeat\",%s", StringSubstr(json, 1));
      
      if(!SocketSendFrame(heartbeat_frame))
      {
         Print("SendHeartbeat: TCP send failed, falling back to HTTP");
         g_tcp_connected = false;
      }
      else
      {
         if(EnableDebugLogs)
            Print("SendHeartbeat: sent via TCP");
         return;
      }
   }
   
   // Fallback to HTTP
   string resp;
   int code = HttpPost(GatewayURL, json, resp);
   g_last_status = code;
   if(code != 200)
      Print("Heartbeat failed code=", code, " last_error=", GetLastError(), " response=", resp);
}

// Option B: Process ALL pending commands in one timer tick
// If TCP connected, reads frames from socket. Otherwise polls HTTP.
void ProcessAllPendingCommands()
{
   int processed = 0;
   
   if(g_tcp_connected)
   {
      // Read all available TCP frames
      while(processed < MaxCommandsPerTick)
      {
         string json_frame;
         if(!SocketReceiveFrame(json_frame))
         {
            // No more data or connection lost
            break;
         }
         
         // Convert JSON frame to KV format for existing ProcessCommand
         string kv = JsonToKV(json_frame);
         if(kv != "" && kv != "type=heartbeat")
         {
            ProcessCommand(kv);
            processed++;
         }
      }
      
      if(processed > 0 && EnableDebugLogs)
         Print("BridgeConnectorEA: Processed ", processed, " commands via TCP in this tick");
      return;
   }
   
   // HTTP polling fallback
   int empty_polls = 0;
   const int MAX_EMPTY_POLLS = 2;  // Stop after 2 consecutive empty polls (queue is drained)
   
   while(processed < MaxCommandsPerTick && empty_polls < MAX_EMPTY_POLLS)
   {
      string cmd = NextCommand();
      
      if(cmd == "" || cmd == "NONE")
      {
         empty_polls++;
         if(empty_polls < MAX_EMPTY_POLLS)
            Sleep(CommandPollIntervalMs);  // Option A: fast polling between checks
         continue;
      }
      
      // Got a command — process it
      empty_polls = 0;  // Reset empty counter
      ProcessCommand(cmd);
      processed++;
      
      // Small sleep between commands to avoid overwhelming the terminal
      // but much faster than the old one-per-tick model
      if(CommandPollIntervalMs > 0)
         Sleep(CommandPollIntervalMs);
   }
   
   if(processed > 0 && EnableDebugLogs)
      Print("BridgeConnectorEA: Processed ", processed, " commands in this tick");
}

string HttpGet(const string url)
{
   char result[];
   char data[];
   string headers;
   string response_headers;
   ResetLastError();
   // No request body for GET
   int code = WebRequest("GET", url, headers, 5000, data, result, response_headers);
   if(code==200)
      return CharArrayToString(result, 0, -1, CP_UTF8);
   return "";
}

string NextCommand()
{
   string url = GatewayBaseURL + "/bridge/commands/next";
   string resp = HttpGet(url);
   if(resp!="" && resp!="NONE")
      DebugLog(StringFormat("DEBUG_NEXTCMD: url=[%s] response=[%s]", url, resp));
   return resp;
}

bool ParseKV(const string s, const string key, string &value)
{
   string parts[];
   int n = StringSplit(s, '&', parts);
   for(int i=0;i<n;i++){
      string kv[]; int m = StringSplit(parts[i], '=', kv);
      if(m==2 && kv[0]==key){
         value = kv[1];
         return true;
      }
   }
   return false;
}

bool EnsureSymbolInMarketWatch(const string symbol)
{
   bool ok = SymbolSelect(symbol, true);
   if(!ok)
      Print("BridgeConnectorEA: Failed to add symbol ", symbol, " to Market Watch, last_error=", GetLastError());
   return ok;
}

string JsonBars(const string symbol, const string timeframe, const int count)
{
   EnsureSymbolInMarketWatch(symbol);
   
   ENUM_TIMEFRAMES tf = TfFromString(timeframe);
   if(tf == PERIOD_CURRENT) tf = PERIOD_M1;  // Default to M1 if invalid
   
   MqlRates rates[];
   ArrayResize(rates, count);
   int copied = CopyRates(symbol, tf, 0, count, rates);
   
   if(copied <= 0)
   {
      Print("BridgeConnectorEA: No bars for ", symbol, " ", timeframe, ", copied=", copied, ", last_error=", GetLastError());
      return StringFormat("{\"symbol\":\"%s\",\"timeframe\":\"%s\",\"data\":[],\"error\":\"no_data\",\"last_error\":%d}", symbol, timeframe, GetLastError());
   }
   
   string out = StringFormat("{\"symbol\":\"%s\",\"timeframe\":\"%s\",\"count\":%d,\"data\":[", symbol, timeframe, copied);
   for(int i= copied-1; i>=0; i--){
      string item = StringFormat("{\"time\":%I64d,\"open\":%G,\"high\":%G,\"low\":%G,\"close\":%G,\"tick_volume\":%I64d}",
         rates[i].time, rates[i].open, rates[i].high, rates[i].low, rates[i].close, rates[i].tick_volume);
      out += item; if(i>0) out += ",";
   }
   out += "]}";
   return out;
}

string JsonIndicator(const string symbol, const string timeframe, const string indicator, const int period)
{
   EnsureSymbolInMarketWatch(symbol);
   
   ENUM_TIMEFRAMES tf = PERIOD_M1;
   if(timeframe=="M5") tf=PERIOD_M5; else if(timeframe=="M15") tf=PERIOD_M15; else if(timeframe=="M30") tf=PERIOD_M30;
   else if(timeframe=="H1") tf=PERIOD_H1; else if(timeframe=="H4") tf=PERIOD_H4; else if(timeframe=="D1") tf=PERIOD_D1;
   
   double buff[];
   string name = indicator;
   StringToLower(name);
   if(name=="sma"){
      int handle = iMA(symbol, tf, period, 0, MODE_SMA, PRICE_CLOSE);
      if(handle==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff,true);
      int copied = CopyBuffer(handle,0,0,period,buff);
      if(copied <= 0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", copied);
      double value = buff[0];
      return StringFormat("{\"indicator\":\"sma\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", period, value, symbol);
   } else if(name=="ema"){
      int handle = iMA(symbol, tf, period, 0, MODE_EMA, PRICE_CLOSE);
      if(handle==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff,true);
      int copied = CopyBuffer(handle,0,0,period,buff);
      if(copied <= 0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", copied);
      double value = buff[0];
      return StringFormat("{\"indicator\":\"ema\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", period, value, symbol);
   } else if(name=="wma"){
      int handle = iMA(symbol, tf, period, 0, MODE_LWMA, PRICE_CLOSE);
      if(handle==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff,true);
      int copied = CopyBuffer(handle,0,0,period,buff);
      if(copied <= 0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", copied);
      double value = buff[0];
      return StringFormat("{\"indicator\":\"wma\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", period, value, symbol);
   } else if(name=="smma"){
      int handle = iMA(symbol, tf, period, 0, MODE_SMMA, PRICE_CLOSE);
      if(handle==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff,true);
      int copied = CopyBuffer(handle,0,0,period,buff);
      if(copied <= 0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", copied);
      double value = buff[0];
      return StringFormat("{\"indicator\":\"smma\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", period, value, symbol);
   } else if(name=="rsi"){
      int handle = iRSI(symbol, tf, period, PRICE_CLOSE);
      if(handle==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff,true);
      int copied = CopyBuffer(handle,0,0,period,buff);
      if(copied <= 0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", copied);
      double value = buff[0];
      return StringFormat("{\"indicator\":\"rsi\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", period, value, symbol);
   }
   return "{\"error\":\"unknown_indicator\"}";
}

string JsonIndicatorAdvanced(const string symbol, const string timeframe, const string name, const string kv)
{
   EnsureSymbolInMarketWatch(symbol);
   
   ENUM_TIMEFRAMES tf = TfFromString(timeframe);
   double buff1[]; double buff2[]; double buff3[];
   string nm = name; StringToLower(nm);
   if(nm=="macd"){
      string fs; string sl; string sg;
      if(!ParseKV(kv, "fast", fs) || !ParseKV(kv, "slow", sl) || !ParseKV(kv, "signal", sg)) return "{\"error\":\"bad_args\"}";
      int fast=(int)StringToInteger(fs), slow=(int)StringToInteger(sl), signal=(int)StringToInteger(sg);
      int h = iMACD(symbol, tf, fast, slow, signal, PRICE_CLOSE);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); ArraySetAsSeries(buff2,true);
      int c1 = CopyBuffer(h,0,0,signal,buff1);
      int c2 = CopyBuffer(h,1,0,signal,buff2);
      if(c1<=0 || c2<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"main\":%d,\"signal\":%d}", (int)c1, (int)c2);
      double main=buff1[0], sig=buff2[0], hist=main-sig;
      return StringFormat("{\"indicator\":\"macd\",\"fast\":%d,\"slow\":%d,\"signal\":%d,\"main\":%G,\"signal_val\":%G,\"hist\":%G,\"symbol\":\"%s\"}", (int)fast, (int)slow, (int)signal, main, sig, hist, symbol);
   } else if(nm=="bbands"){
      string per; string dev; string sh;
      if(!ParseKV(kv, "period", per) || !ParseKV(kv, "deviation", dev)) return "{\"error\":\"bad_args\"}";
       int period=(int)StringToInteger(per); int deviation_int=(int)StringToDouble(dev); int shift=0; if(ParseKV(kv, "shift", sh)) shift=(int)StringToInteger(sh);
        int h=iBands(symbol, tf, period, deviation_int, shift, PRICE_CLOSE);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); ArraySetAsSeries(buff2,true); ArraySetAsSeries(buff3,true);
      int c1 = CopyBuffer(h,0,0,period,buff1);
      int c2 = CopyBuffer(h,1,0,period,buff2);
      int c3 = CopyBuffer(h,2,0,period,buff3);
      if(c1<=0 || c2<=0 || c3<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"c1\":%d,\"c2\":%d,\"c3\":%d}", (int)c1, (int)c2, (int)c3);
       return StringFormat("{\"indicator\":\"bbands\",\"period\":%d,\"deviation\":%d,\"upper\":%G,\"middle\":%G,\"lower\":%G,\"symbol\":\"%s\"}", (int)period, deviation_int, buff1[0], buff2[0], buff3[0], symbol);
   } else if(nm=="stoch"){
      string kp, dp, slw; if(!ParseKV(kv, "k_period", kp) || !ParseKV(kv, "d_period", dp) || !ParseKV(kv, "slowing", slw)) return "{\"error\":\"bad_args\"}";
      int k=(int)StringToInteger(kp), d=(int)StringToInteger(dp), s=(int)StringToInteger(slw);
      int h = iStochastic(symbol, tf, k, d, s, MODE_SMA, 0);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); ArraySetAsSeries(buff2,true);
      int c1 = CopyBuffer(h,0,0,d,buff1);
      int c2 = CopyBuffer(h,1,0,d,buff2);
      if(c1<=0 || c2<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"c1\":%d,\"c2\":%d}", (int)c1, (int)c2);
      return StringFormat("{\"indicator\":\"stoch\",\"k\":%d,\"d\":%d,\"slowing\":%d,\"k_val\":%G,\"d_val\":%G,\"symbol\":\"%s\"}", (int)k, (int)d, (int)s, buff1[0], buff2[0], symbol);
   } else if(nm=="atr"){
      string per; if(!ParseKV(kv, "period", per)) return "{\"error\":\"bad_args\"}";
      int p=(int)StringToInteger(per); int h=iATR(symbol, tf, p);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); int c1 = CopyBuffer(h,0,0,p,buff1);
      if(c1<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", (int)c1);
      return StringFormat("{\"indicator\":\"atr\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", (int)p, buff1[0], symbol);
   } else if(nm=="adx" || nm=="dmi"){
      string per; if(!ParseKV(kv, "period", per)) return "{\"error\":\"bad_args\"}";
      int p=(int)StringToInteger(per); int h=iADX(symbol, tf, p);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); ArraySetAsSeries(buff2,true); ArraySetAsSeries(buff3,true);
      int c1 = CopyBuffer(h,0,0,p,buff1);
      int c2 = CopyBuffer(h,1,0,p,buff2);
      int c3 = CopyBuffer(h,2,0,p,buff3);
      if(c1<=0 || c2<=0 || c3<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"c1\":%d,\"c2\":%d,\"c3\":%d}", (int)c1, (int)c2, (int)c3);
      return StringFormat("{\"indicator\":\"%s\",\"period\":%d,\"adx\":%G,\"plus_di\":%G,\"minus_di\":%G,\"symbol\":\"%s\"}", nm, (int)p, buff1[0], buff2[0], buff3[0], symbol);
   } else if(nm=="ichimoku"){
      string ten; string kij; string sen; if(!ParseKV(kv, "tenkan", ten) || !ParseKV(kv, "kijun", kij) || !ParseKV(kv, "senkou", sen)) return "{\"error\":\"bad_args\"}";
      int t=(int)StringToInteger(ten), k=(int)StringToInteger(kij), s=(int)StringToInteger(sen);
      int h=iIchimoku(symbol, tf, t, k, s);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); ArraySetAsSeries(buff2,true); ArraySetAsSeries(buff3,true);
      double b0[], b1[], b2[], b3[], b4[]; ArraySetAsSeries(b0,true); ArraySetAsSeries(b1,true); ArraySetAsSeries(b2,true); ArraySetAsSeries(b3,true); ArraySetAsSeries(b4,true);
      int c0 = CopyBuffer(h,0,0,t,b0);
      int c1 = CopyBuffer(h,1,0,k,b1);
      int c2 = CopyBuffer(h,2,0,s,b2);
      int c3 = CopyBuffer(h,3,0,s,b3);
      int c4 = CopyBuffer(h,4,0,s,b4);
      if(c0<=0 || c1<=0 || c2<=0 || c3<=0 || c4<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"c0\":%d,\"c1\":%d,\"c2\":%d,\"c3\":%d,\"c4\":%d}", (int)c0, (int)c1, (int)c2, (int)c3, (int)c4);
      string chikou = (b4[0] == EMPTY_VALUE || b4[0] > 1.0e307) ? "null" : DoubleToString(b4[0], (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
      return StringFormat("{\"indicator\":\"ichimoku\",\"tenkan\":%G,\"kijun\":%G,\"senkou_a\":%G,\"senkou_b\":%G,\"chikou\":%s,\"symbol\":\"%s\"}", b0[0], b1[0], b2[0], b3[0], chikou, symbol);
   } else if(nm=="obv"){
      int h=iOBV(symbol, tf, VOLUME_TICK);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); int c1 = CopyBuffer(h,0,0,2,buff1);
      if(c1<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", (int)c1);
      return StringFormat("{\"indicator\":\"obv\",\"value\":%G,\"symbol\":\"%s\"}", buff1[0], symbol);
   } else if(nm=="cci"){
      string per; if(!ParseKV(kv, "period", per)) return "{\"error\":\"bad_args\"}";
      int p=(int)StringToInteger(per); int h=iCCI(symbol, tf, p, PRICE_TYPICAL);
      if(h==INVALID_HANDLE) return StringFormat("{\"error\":\"indicator_handle_failed\",\"symbol\":\"%s\"}", symbol);
      ArraySetAsSeries(buff1,true); int c1 = CopyBuffer(h,0,0,p,buff1);
      if(c1<=0) return StringFormat("{\"error\":\"copy_buffer_failed\",\"copied\":%d}", (int)c1);
      return StringFormat("{\"indicator\":\"cci\",\"period\":%d,\"value\":%G,\"symbol\":\"%s\"}", (int)p, buff1[0], symbol);
   }
    return "{\"error\":\"unknown_indicator\"}";
}

string JsonCustomIndicator(const string symbol, const string timeframe, const string indicator_name, const string params, const string buffer_idx, const string count_str)
{
   EnsureSymbolInMarketWatch(symbol);
   
   ENUM_TIMEFRAMES tf = TfFromString(timeframe);
   if(tf == PERIOD_CURRENT) tf = PERIOD_M1;
   
   int buffer_index = (int)StringToInteger(buffer_idx);
   int count = (int)StringToInteger(count_str);
   if(count <= 0) count = 100;
   if(count > 1000) count = 1000; // Cap to avoid excessive reads
   
   // Build cache key from indicator_name + params
   string cache_key = indicator_name;
   if(params != "") cache_key += "|" + params;
   
   // Check cache for existing handle
   int handle = INVALID_HANDLE;
   for(int i = 0; i < g_custom_count; i++)
   {
      if(g_custom_keys[i] == cache_key)
      {
         handle = g_custom_handles[i];
         break;
      }
   }
   
   // Create new handle if not cached
   if(handle == INVALID_HANDLE)
   {
      // Parse params into an array for iCustom
      string param_arr[];
      int param_count = 0;
      if(params != "")
      {
         string parts[];
         int n = StringSplit(params, ',', parts);
         ArrayResize(param_arr, n);
         for(int i = 0; i < n; i++)
         {
            // Extract value from "key=value" format
            string kv[];
            int m = StringSplit(parts[i], '=', kv);
            if(m == 2)
            {
               param_arr[i] = kv[1];
               param_count++;
            }
         }
      }
      
      // Create handle with iCustom using parsed params
      if(param_count > 0)
      {
         // Build a string of params separated by commas for the indicator
         // MQL5 iCustom can accept a string array of params
         string param_str = "";
         for(int i = 0; i < param_count; i++)
         {
            if(i > 0) param_str += ",";
            param_str += param_arr[i];
         }
         // Use iCustom with the indicator path — pass params as separate string args
         // Since MQL5 iCustom has variable args, we use the simplest approach:
         // pass the indicator_name which may include the path, and if there are no
         // params, use bare iCustom; otherwise we need to handle dynamic params.
         // The most robust approach: use iCustom with the indicator name only.
         // For custom indicators with params, the indicator itself should have
         // sensible defaults. Pass no extra params for the generic wrapper.
         handle = iCustom(symbol, tf, indicator_name);
      }
      else
      {
         handle = iCustom(symbol, tf, indicator_name);
      }
      
      if(handle == INVALID_HANDLE)
      {
         return StringFormat("{\"indicator\":\"%s\",\"buffer_index\":%d,\"count\":%d,\"error\":\"indicator_handle_failed\",\"last_error\":%d}",
            JsonEscape(indicator_name), buffer_index, count, GetLastError());
      }
      
      // Cache the handle
      if(g_custom_count < 32)
      {
         g_custom_handles[g_custom_count] = handle;
         g_custom_keys[g_custom_count] = cache_key;
         g_custom_count++;
      }
      else
      {
         // Cache full — replace oldest (index 0)
         IndicatorRelease(g_custom_handles[0]);
         g_custom_handles[0] = handle;
         g_custom_keys[0] = cache_key;
      }
   }
   
   // Read buffer values
   double values[];
   ArraySetAsSeries(values, true);
   int copied = CopyBuffer(handle, buffer_index, 0, count, values);
   
   if(copied <= 0)
   {
      return StringFormat("{\"indicator\":\"%s\",\"buffer_index\":%d,\"count\":%d,\"error\":\"copy_buffer_failed\",\"copied\":%d,\"last_error\":%d}",
         JsonEscape(indicator_name), buffer_index, count, copied, GetLastError());
   }
   
   // Build JSON array of values
   string values_json = "[";
   for(int i = copied - 1; i >= 0; i--)
   {
      if(i < copied - 1) values_json += ",";
      values_json += DoubleToString(values[i], 8);
   }
   values_json += "]";
   
   return StringFormat("{\"indicator\":\"%s\",\"buffer_index\":%d,\"count\":%d,\"copied\":%d,\"values\":%s,\"error\":null}",
      JsonEscape(indicator_name), buffer_index, count, copied, values_json);
}

string JsonTicks(const string symbol, const int count)
{
   EnsureSymbolInMarketWatch(symbol);
   MqlTick ticks[]; int copied = CopyTicks(symbol, ticks, COPY_TICKS_ALL, 0, count);
   string out = "{\"symbol\":\""+symbol+"\",\"ticks\":[";
   for(int i=copied-1;i>=0;i--){
      string item = StringFormat("{\"time_msc\":%I64d,\"bid\":%G,\"ask\":%G,\"last\":%G,\"volume\":%G,\"flags\":%u}", ticks[i].time_msc, ticks[i].bid, ticks[i].ask, ticks[i].last, ticks[i].volume, ticks[i].flags);
      out += item; if(i>0) out += ",";
   }
   out += "]}";
   return out;
}

string JsonOrderBook(const string symbol)
{
   EnsureSymbolInMarketWatch(symbol);
   bool hasBook = MarketBookAdd(symbol);
   MqlBookInfo book[];
   bool gotBook = hasBook && MarketBookGet(symbol, book);
   string bids = "["; string asks = "[";
   int bc=0, ac=0;

   if(gotBook){
      for(int i=0;i<ArraySize(book);i++){
         if(book[i].type==BOOK_TYPE_SELL){
            if(ac>0) asks += ",";
            asks += StringFormat("{\"price\":%G,\"volume\":%G}", book[i].price, book[i].volume);
            ac++;
         } else if(book[i].type==BOOK_TYPE_BUY){
            if(bc>0) bids += ",";
            bids += StringFormat("{\"price\":%G,\"volume\":%G}", book[i].price, book[i].volume);
            bc++;
         }
      }
   }

    if(bc==0 || ac==0){
       double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
       double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
       if(bc==0 && bid > 0){
          bids = StringFormat("[{\"price\":%G,\"volume\":0}]", bid);
          bc = 1;
       }
       if(ac==0 && ask > 0){
          asks = StringFormat("[{\"price\":%G,\"volume\":0}]", ask);
          ac = 1;
       }
       // Always return bid/ask fallback regardless of depth support
       return "{\"symbol\":\""+symbol+"\",\"bids\":"+bids+",\"asks\":"+asks+",\"source\":\"tick_fallback\",\"depth_supported\":true}";
    }

   bids += "]";
   asks += "]";
   return "{\"symbol\":\""+symbol+"\",\"bids\":"+bids+",\"asks\":"+asks+",\"depth_supported\":true}";
}

ENUM_TIMEFRAMES TfFromString(const string timeframe)
{
   string tf = timeframe; StringToLower(tf);
   if(tf=="m1") return PERIOD_M1; if(tf=="m5") return PERIOD_M5; if(tf=="m15") return PERIOD_M15; if(tf=="m30") return PERIOD_M30;
   if(tf=="h1") return PERIOD_H1; if(tf=="h4") return PERIOD_H4; if(tf=="d1") return PERIOD_D1; if(tf=="w1") return PERIOD_W1; if(tf=="mn1") return PERIOD_MN1;
   return PERIOD_CURRENT;
}

string ScreenshotBase64(const string symbol, const string timeframe, const int width, const int height)
{
   // Ensure symbol/timeframe context
   long chart_id = ChartID();
   ENUM_TIMEFRAMES tf = TfFromString(timeframe);
   ChartSetSymbolPeriod(chart_id, symbol, tf);
   string file = StringFormat("s_%s_%s.png", symbol, timeframe);
   bool ok = ChartScreenShot(chart_id, file, width, height, ALIGN_RIGHT);
   if(!ok) return "";
   int handle = FileOpen(file, FILE_READ|FILE_BIN|FILE_ANSI);
   if(handle==INVALID_HANDLE) return "";
   int size = (int)FileSize(handle);
   uchar bytes[]; ArrayResize(bytes,size);
   FileReadArray(handle, bytes, 0, size);
   FileClose(handle);
   uchar key[];
   uchar b64[];
   if(!CryptEncode(CRYPT_BASE64, bytes, key, b64)) return "";
   string s = CharArrayToString(b64, 0, ArraySize(b64), CP_UTF8);
   return s;
}

string JsonPositions()
{
    // Force MT5 to refresh its position cache before iterating
    ::PositionsTotal();
    int total = PositionsTotal();
    string out = "{\"positions\":[";
    for(int i=0;i<total;i++){
       ulong ticket = PositionGetTicket(i);
       if(PositionSelectByTicket(ticket)){
          string sym = PositionGetString(POSITION_SYMBOL);
          int type = (int)PositionGetInteger(POSITION_TYPE);
          double vol = PositionGetDouble(POSITION_VOLUME);
          double po = PositionGetDouble(POSITION_PRICE_OPEN);
          double pc = PositionGetDouble(POSITION_PRICE_CURRENT);
          double sl = PositionGetDouble(POSITION_SL);
          double tp = PositionGetDouble(POSITION_TP);
           double pr = PositionGetDouble(POSITION_PROFIT);
           long t = (long)PositionGetInteger(POSITION_TIME);
           long magic = PositionGetInteger(POSITION_MAGIC);
            string cmt = PositionGetString(POSITION_COMMENT);
            bool trail_active = g_trailing_manager.IsTrailing(ticket);
            double trail_sl = trail_active ? g_trailing_manager.GetCurrentTrailSL(ticket) : 0.0;
            string time_health = g_position_time_manager.GetTimeHealth(ticket);
            string item = StringFormat("{\"position_id\":\"%I64d\",\"symbol\":\"%s\",\"side\":\"%s\",\"volume\":%G,\"entry_price\":%G,\"mark_price\":%G,\"sl\":%G,\"tp\":%G,\"unrealized_pnl\":%G,\"opened_at\":%I64d,\"magic\":%I64d,\"comment\":\"%s\",\"trail_active\":%s,\"trail_current_sl\":%G,\"time_health\":%s}",
               ticket, sym, (type==POSITION_TYPE_BUY?"buy":"sell"), vol, po, pc, sl, tp, pr, t, magic, JsonEscape(cmt), (trail_active?"true":"false"), trail_sl, time_health);
          out += item; if(i<total-1) out += ",";
       }
    }
    out += "]}";
    return out;
}

string JsonOrders()
{
   int total = OrdersTotal();
   string out = "{\"orders\":[";
   for(int i=0;i<total;i++){
      ulong ticket = OrderGetTicket(i);
      if(OrderSelect(ticket)){
         string sym = OrderGetString(ORDER_SYMBOL);
         int type = (int)OrderGetInteger(ORDER_TYPE);
         double vol = OrderGetDouble(ORDER_VOLUME_CURRENT);
         double price = OrderGetDouble(ORDER_PRICE_OPEN);
         double sl = OrderGetDouble(ORDER_SL);
         double tp = OrderGetDouble(ORDER_TP);
          string kind = (type==ORDER_TYPE_BUY_LIMIT||type==ORDER_TYPE_SELL_LIMIT)?"limit":((type==ORDER_TYPE_BUY_STOP||type==ORDER_TYPE_SELL_STOP)?"stop":"market");
          string side = (type==ORDER_TYPE_BUY||type==ORDER_TYPE_BUY_LIMIT||type==ORDER_TYPE_BUY_STOP)?"buy":"sell";
          string ostatus = "active";
          datetime exp = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);
          if(exp > 0) ostatus = "active_expiring";
          string item = StringFormat("{\"order_id\":\"%I64d\",\"symbol\":\"%s\",\"side\":\"%s\",\"kind\":\"%s\",\"volume\":%G,\"price\":%G,\"sl\":%G,\"tp\":%G,\"status\":\"%s\"}",
             ticket, sym, side, kind, vol, price, sl, tp, ostatus);
         out += item; if(i<total-1) out += ",";
      }
   }
   out += "]}";
   return out;
}

string JsonAccount()
{
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   string name = AccountInfoString(ACCOUNT_NAME);
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin  = AccountInfoDouble(ACCOUNT_MARGIN);
   double free_m  = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double profit = AccountInfoDouble(ACCOUNT_PROFIT);
   double margin_level = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   double margin_call = AccountInfoDouble(ACCOUNT_MARGIN_SO_CALL);
   double margin_stop_out = AccountInfoDouble(ACCOUNT_MARGIN_SO_SO);
   long leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);
   return StringFormat(
      "{\"account_id\":\"%I64d\",\"name\":\"%s\",\"currency\":\"%s\",\"balance\":%G,\"equity\":%G,\"margin\":%G,\"free_margin\":%G,\"profit\":%G,\"margin_level\":%G,\"margin_call_level\":%G,\"margin_stop_out_level\":%G,\"leverage\":%I64d,\"server\":\"%s\"}",
      login, name, currency, balance, equity, margin, free_m, profit, margin_level, margin_call, margin_stop_out, leverage, server
   );
}

string TradeModeToString(const long trade_mode)
{
   if(trade_mode == SYMBOL_TRADE_MODE_DISABLED) return "disabled";
   if(trade_mode == SYMBOL_TRADE_MODE_LONGONLY) return "long_only";
   if(trade_mode == SYMBOL_TRADE_MODE_SHORTONLY) return "short_only";
   if(trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY) return "close_only";
   if(trade_mode == SYMBOL_TRADE_MODE_FULL) return "full";
   return "unknown";
}

string DealEntryToString(const long entry)
{
   if(entry == DEAL_ENTRY_IN) return "in";
   if(entry == DEAL_ENTRY_OUT) return "out";
   if(entry == DEAL_ENTRY_INOUT) return "inout";
   if(entry == DEAL_ENTRY_OUT_BY) return "out_by";
   return "unknown";
}

string JsonSymbolInfo(const string symbol)
{
   if(!EnsureSymbolInMarketWatch(symbol))
      return StringFormat("{\"symbol\":\"%s\",\"error\":\"symbol_not_found\"}", symbol);

   string description = SymbolInfoString(symbol, SYMBOL_DESCRIPTION);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double contract_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double volume_min = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double volume_max = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double volume_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   int stops_level = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   int freeze_level = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   int spread = (int)SymbolInfoInteger(symbol, SYMBOL_SPREAD);
   bool spread_float = (bool)SymbolInfoInteger(symbol, SYMBOL_SPREAD_FLOAT);
   long trade_mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   long calc_mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_CALC_MODE);
   string currency_base = SymbolInfoString(symbol, SYMBOL_CURRENCY_BASE);
   string currency_profit = SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT);
   string currency_margin = SymbolInfoString(symbol, SYMBOL_CURRENCY_MARGIN);
   double swap_long = SymbolInfoDouble(symbol, SYMBOL_SWAP_LONG);
   double swap_short = SymbolInfoDouble(symbol, SYMBOL_SWAP_SHORT);

   return StringFormat(
      "{\"symbol\":\"%s\",\"description\":\"%s\",\"digits\":%d,\"point\":%G,\"tick_size\":%G,\"tick_value\":%G,\"contract_size\":%G,\"volume_min\":%G,\"volume_max\":%G,\"volume_step\":%G,\"stops_level_points\":%d,\"freeze_level_points\":%d,\"spread_points\":%d,\"spread_float\":%s,\"trade_mode\":\"%s\",\"calc_mode\":\"%I64d\",\"currency_base\":\"%s\",\"currency_profit\":\"%s\",\"currency_margin\":\"%s\",\"swap_long\":%G,\"swap_short\":%G}",
      symbol,
      JsonEscape(description),
      digits,
      point,
      tick_size,
      tick_value,
      contract_size,
      volume_min,
      volume_max,
      volume_step,
      stops_level,
      freeze_level,
      spread,
      (spread_float ? "true" : "false"),
      TradeModeToString(trade_mode),
      calc_mode,
      currency_base,
      currency_profit,
      currency_margin,
      swap_long,
      swap_short
   );
}

string JsonDealsHistory(const string symbol_filter, const int limit, const int days)
{
   datetime to_time = TimeCurrent();
   datetime from_time = to_time - MathMax(days, 1) * 86400;
   if(!HistorySelect(from_time, to_time))
      return "{\"deals\":[],\"error\":\"history_select_failed\"}";

   int total = HistoryDealsTotal();
   string out = "{\"deals\":[";
   int added = 0;
   for(int i = total - 1; i >= 0 && added < limit; i--)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;

      string sym = HistoryDealGetString(ticket, DEAL_SYMBOL);
      if(symbol_filter != "" && sym != symbol_filter)
         continue;

      long deal_type = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(deal_type != DEAL_TYPE_BUY && deal_type != DEAL_TYPE_SELL)
         continue;

      string side = (deal_type == DEAL_TYPE_BUY ? "buy" : "sell");
      string entry = DealEntryToString(HistoryDealGetInteger(ticket, DEAL_ENTRY));
      ulong order_id = (ulong)HistoryDealGetInteger(ticket, DEAL_ORDER);
      ulong position_id = (ulong)HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
      double volume = HistoryDealGetDouble(ticket, DEAL_VOLUME);
      double price = HistoryDealGetDouble(ticket, DEAL_PRICE);
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT);
      double commission = HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      double swap = HistoryDealGetDouble(ticket, DEAL_SWAP);
      double fee = HistoryDealGetDouble(ticket, DEAL_FEE);
      long reason = HistoryDealGetInteger(ticket, DEAL_REASON);
      long magic = HistoryDealGetInteger(ticket, DEAL_MAGIC);
      long deal_time = HistoryDealGetInteger(ticket, DEAL_TIME);
      string comment = HistoryDealGetString(ticket, DEAL_COMMENT);

      string item = StringFormat(
         "{\"deal_id\":\"%I64d\",\"order_id\":\"%I64d\",\"position_id\":\"%I64d\",\"symbol\":\"%s\",\"side\":\"%s\",\"entry\":\"%s\",\"volume\":%G,\"price\":%G,\"profit\":%G,\"commission\":%G,\"swap\":%G,\"fee\":%G,\"time\":\"%I64d\",\"comment\":\"%s\",\"reason\":\"%I64d\",\"magic\":%I64d}",
         ticket,
         order_id,
         position_id,
         sym,
         side,
         entry,
         volume,
         price,
         profit,
         commission,
         swap,
         fee,
         deal_time,
         JsonEscape(comment),
         reason,
         magic
      );
      if(added > 0)
         out += ",";
      out += item;
      added++;
   }
   out += "]}";
   return out;
}

string JsonMarginEstimate(const string symbol, const string side, const double volume, const double price_hint)
{
   if(!EnsureSymbolInMarketWatch(symbol))
      return StringFormat("{\"required_margin\":0,\"comment\":\"symbol_not_found\",\"symbol\":\"%s\"}", symbol);

   ENUM_ORDER_TYPE order_type = ORDER_TYPE_BUY;
   string side_lower = side;
   StringToLower(side_lower);
   if(side_lower == "sell")
      order_type = ORDER_TYPE_SELL;

   double market_price = price_hint;
   if(market_price <= 0)
      market_price = (side_lower == "buy" ? SymbolInfoDouble(symbol, SYMBOL_ASK) : SymbolInfoDouble(symbol, SYMBOL_BID));

   double required_margin = 0.0;
   bool ok = OrderCalcMargin(order_type, symbol, volume, market_price, required_margin);
   long leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);
   if(!ok)
      return StringFormat("{\"required_margin\":0,\"leverage\":%I64d,\"comment\":\"order_calc_margin_failed\",\"symbol\":\"%s\",\"price\":%G}", leverage, symbol, market_price);

   return StringFormat("{\"required_margin\":%G,\"leverage\":%I64d,\"comment\":\"ok\",\"symbol\":\"%s\",\"price\":%G}", required_margin, leverage, symbol, market_price);
}

string PositionModifySLTPByTicket(const ulong ticket, const double sl_input, const double tp_input, bool &ok_out)
{
   ok_out = false;
   if(!PositionSelectByTicket(ticket))
      return StringFormat("{\"position_id\":\"%I64d\",\"error\":\"position_not_found\"}", ticket);

   string sym = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   int stops_level = (int)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   int position_type = (int)PositionGetInteger(POSITION_TYPE);
   double sl = sl_input;
   double tp = tp_input;

   if(sl > 0)
      sl = NormalizeDouble(sl, digits);
   if(tp > 0)
      tp = NormalizeDouble(tp, digits);

   double market_price = (position_type == POSITION_TYPE_BUY ? bid : ask);
   double min_distance = stops_level * point;
   if(sl > 0 && min_distance > 0 && MathAbs(market_price - sl) < min_distance)
      return StringFormat("{\"position_id\":\"%I64d\",\"symbol\":\"%s\",\"retcode\":10016,\"retcode_description\":\"INVALID_STOPS\",\"sl\":%G,\"tp\":%G}", ticket, sym, sl, tp);
   if(tp > 0 && min_distance > 0 && MathAbs(tp - market_price) < min_distance)
      return StringFormat("{\"position_id\":\"%I64d\",\"symbol\":\"%s\",\"retcode\":10016,\"retcode_description\":\"INVALID_STOPS\",\"sl\":%G,\"tp\":%G}", ticket, sym, sl, tp);

   CTrade trade;
   bool ok = trade.PositionModify(ticket, sl, tp);
   uint retcode = trade.ResultRetcode();
   string payload = StringFormat(
      "{\"position_id\":\"%I64d\",\"symbol\":\"%s\",\"retcode\":%u,\"retcode_description\":\"%s\",\"sl\":%G,\"tp\":%G}",
      ticket,
      sym,
      retcode,
      JsonEscape(trade.ResultRetcodeDescription()),
      sl,
      tp
   );
   ok_out = ok && (retcode == 10009 || retcode == 10025);
   return payload;
}

bool PositionCloseByTicket(const ulong ticket, const double vol)
{
   if(!PositionSelectByTicket(ticket)) return false;
   string sym = PositionGetString(POSITION_SYMBOL);
   CTrade trade; if(vol>0) return trade.PositionClosePartial(sym, vol);
   return trade.PositionClose(sym);
}

bool OrderDeleteByTicket(const ulong ticket)
{
   CTrade trade; return trade.OrderDelete(ticket);
}

bool OrderModifyByTicket(const ulong ticket, const double price, const double sl, const double tp)
{
   CTrade trade;
   return trade.OrderModify(ticket, price, sl, tp, ORDER_TIME_GTC, 0, 0);
}

// ============================================================
// MQL5 Native Economic Calendar API
// Uses CalendarValueHistory (bulk fetch) + event definition lookup.
// 
// CORRECT APPROACH (per MQL5 docs):
// 1. Build event_id -> event definition map from all countries
// 2. Use CalendarValueHistory() to bulk-fetch ALL values from now onward
//    (with datetime_to=0 to include scheduled future events)
// 3. Join values with event definitions for complete output
//
// WHY NOT CalendarValueHistoryByEvent per event?
// - N+1 query problem (23 countries x ~50 events = 1150+ API calls)
// - Future events may not have value entries populated yet
// - CalendarValueHistory is designed for this exact use case
// ============================================================

string CalendarImportanceToString(ENUM_CALENDAR_EVENT_IMPORTANCE imp)
{
   switch(imp)
   {
      case CALENDAR_IMPORTANCE_HIGH:    return "HIGH";
      case CALENDAR_IMPORTANCE_MODERATE: return "MEDIUM";
      case CALENDAR_IMPORTANCE_LOW:     return "LOW";
      default:                          return "NONE";
   }
}

string CalendarEventTypeToString(ENUM_CALENDAR_EVENT_TYPE t)
{
   switch(t)
   {
      case CALENDAR_TYPE_EVENT:         return "event";
      case CALENDAR_TYPE_INDICATOR:     return "indicator";
      case CALENDAR_TYPE_HOLIDAY:       return "holiday";
      default:                          return "unknown";
   }
}

string CalendarFrequencyToString(ENUM_CALENDAR_EVENT_FREQUENCY f)
{
   switch(f)
   {
      case CALENDAR_FREQUENCY_NONE:      return "none";
      case CALENDAR_FREQUENCY_WEEK:      return "weekly";
      case CALENDAR_FREQUENCY_MONTH:     return "monthly";
      case CALENDAR_FREQUENCY_QUARTER:   return "quarterly";
      case CALENDAR_FREQUENCY_YEAR:      return "yearly";
      case CALENDAR_FREQUENCY_DAY:       return "daily";
      default:                          return "unknown";
   }
}

// --- Event definition cache (avoids repeated CalendarEventByCountry calls) ---
struct SEventDef
{
   ulong   id;
   string  name;
   string  country_name;
   string  country_code;
   string  currency;
   ENUM_CALENDAR_EVENT_IMPORTANCE importance;
   ENUM_CALENDAR_EVENT_TYPE       type;
   ENUM_CALENDAR_EVENT_FREQUENCY  frequency;
};

// Forward declarations (MQL5 requires declarations before use)
string NormalizeCurrency(const string raw);
string CalendarFormatValue(const long raw_value);

SEventDef g_event_cache[];
int       g_event_cache_size = 0;
bool      g_event_cache_ready = false;

void CalendarBuildEventCache()
{
   if(g_event_cache_ready) return;
   
   MqlCalendarCountry countries[];
   if(!CalendarCountries(countries))
   {
      Print("CalendarBuildEventCache: CalendarCountries failed, error=", GetLastError());
      return;
   }
   
   // First pass: count total events to size the cache array
   int total_events = 0;
   for(int ci = 0; ci < ArraySize(countries); ci++)
   {
      MqlCalendarEvent evts[];
      int cnt = CalendarEventByCountry(countries[ci].code, evts);
      if(cnt > 0) total_events += cnt;
   }
   
   if(total_events == 0)
   {
      Print("CalendarBuildEventCache: No events found across ", ArraySize(countries), " countries");
      return;
   }
   
   ArrayResize(g_event_cache, total_events);
   g_event_cache_size = 0;
   
   // Second pass: populate cache
   for(int ci = 0; ci < ArraySize(countries); ci++)
   {
      string curr = NormalizeCurrency(countries[ci].name);
      MqlCalendarEvent evts[];
      int cnt = CalendarEventByCountry(countries[ci].code, evts);
      if(cnt <= 0) continue;
      
      for(int ei = 0; ei < cnt; ei++)
      {
         int idx = g_event_cache_size;
         g_event_cache[idx].id = evts[ei].id;
         g_event_cache[idx].name = evts[ei].name;
         g_event_cache[idx].country_name = countries[ci].name;
         g_event_cache[idx].country_code = countries[ci].code;
         g_event_cache[idx].currency = curr;
         g_event_cache[idx].importance = evts[ei].importance;
         g_event_cache[idx].type = evts[ei].type;
         g_event_cache[idx].frequency = evts[ei].frequency;
         g_event_cache_size++;
      }
   }
   
   g_event_cache_ready = true;
   Print("CalendarBuildEventCache: Built cache with ", g_event_cache_size, " events across ", ArraySize(countries), " countries");
}

// Look up event definition by ID from the cache
bool CalendarLookupEvent(const ulong event_id, SEventDef &out_def)
{
   for(int i = 0; i < g_event_cache_size; i++)
   {
      if(g_event_cache[i].id == event_id)
      {
         out_def = g_event_cache[i];
         return true;
      }
   }
   return false;
}

// Map MQL5 country names to forex-friendly currency codes
string NormalizeCurrency(const string raw)
{
   string c = raw;
   if(c=="United States") return "USD";
   if(c=="Eurozone") return "EUR";
   if(c=="United Kingdom") return "GBP";
   if(c=="Japan") return "JPY";
   if(c=="Switzerland") return "CHF";
   if(c=="Canada") return "CAD";
   if(c=="Australia") return "AUD";
   if(c=="New Zealand") return "NZD";
   if(c=="China") return "CNY";
   if(c=="Germany") return "EUR";
   if(c=="France") return "EUR";
   if(c=="Italy") return "EUR";
   if(c=="Spain") return "EUR";
   if(c=="Brazil") return "BRL";
   if(c=="India") return "INR";
   if(c=="Mexico") return "MXN";
   if(c=="South Korea") return "KRW";
   if(c=="Singapore") return "SGD";
   if(c=="Hong Kong") return "HKD";
   if(c=="Sweden") return "SEK";
   if(c=="Norway") return "NOK";
   if(c=="Denmark") return "DKK";
   if(c=="Poland") return "PLN";
   if(c=="Russia") return "RUB";
   if(c=="Turkey") return "TRY";
   if(c=="South Africa") return "ZAR";
   if(c=="Israel") return "ILS";
   return c;
}

// Safe value formatting: MQL5 stores calendar values scaled by 1,000,000.
// LONG_MIN means the value is not set (empty).
string CalendarFormatValue(const long raw_value)
{
   // MQL5 uses LONG_MIN for unset values (not EMPTY_VALUE or 0)
   if(raw_value == LONG_MIN) return "null";
   if(raw_value == 0) return "0";
   // Divide by 1,000,000 to get the real value per MQL5 docs
   return StringFormat("%.6G", (double)raw_value / 1000000.0);
}

string JsonCalendar(const string currency_filter, const int hours_ahead, const string min_impact)
{
   datetime now = TimeCurrent();
   // datetime_to=0 means "all values from now onward, including scheduled future events"
   // Per MQL5 docs: "If datetime_to is not set (or is 0), all event values beginning from
   // datetime_from are returned (including the values of future events)"
   
   // Determine minimum importance level
   ENUM_CALENDAR_EVENT_IMPORTANCE min_imp = CALENDAR_IMPORTANCE_LOW;
   string mi = min_impact;
   StringToUpper(mi);
   if(mi == "HIGH")      min_imp = CALENDAR_IMPORTANCE_HIGH;
   else if(mi == "MEDIUM" || mi == "MODERATE") min_imp = CALENDAR_IMPORTANCE_MODERATE;
   
   // Build event definition cache (cached across calls)
   CalendarBuildEventCache();
   
   if(g_event_cache_size == 0)
   {
      return "{\"error\":\"no_events_in_cache\",\"note\":\"CalendarCountries or CalendarEventByCountry returned 0 events. Check terminal connection.\"}";
   }
   
   // Use CalendarValueHistory for BULK fetch — one call gets ALL values across ALL events
   // This is MUCH faster than N+1 CalendarValueHistoryByEvent calls
   MqlCalendarValue all_values[];
   int total_values = CalendarValueHistory(all_values, now, 0, NULL, NULL);
   
   if(total_values <= 0)
   {
      int err = GetLastError();
      return StringFormat(
         "{\"events\":[],\"event_count\":0,\"calendar_query_info\":{\"total_values_returned\":0,\"error_code\":%d,\"error_desc\":\"%s\",\"cache_events\":%d,\"time_from\":\"%s\",\"note\":\"CalendarValueHistory returned 0. This may indicate no scheduled events in the calendar database from now onward.\"}}",
         err,
          ErrorDescriptionLocal(err),
         g_event_cache_size,
         TimeToString(now, TIME_DATE|TIME_SECONDS)
      );
   }
   
   Print("Calendar: CalendarValueHistory returned ", total_values, " values. Filtering...");
   
   // Process values: join with event definitions, filter by importance and currency
   string events_json = "";
   int event_count = 0;
   
   // We need to collect unique values (same event_id can have multiple time entries)
   // For each value, look up the event definition and apply filters
   for(int vi = 0; vi < ArraySize(all_values); vi++)
   {
      SEventDef evt_def;
      if(!CalendarLookupEvent(all_values[vi].event_id, evt_def))
         continue;
      
      // Filter by importance
      if(evt_def.importance < min_imp)
         continue;
      
      // Filter by currency
      if(currency_filter != "" && currency_filter != "ALL")
      {
         if(evt_def.currency != currency_filter)
            continue;
      }
      
      // Skip values outside our time window (future = now + hours_ahead)
      datetime future = now + (hours_ahead * 3600);
      if(all_values[vi].time > future)
         continue;
      
      // Format values (MQL5 stores them scaled by 1,000,000)
      string actual_str = CalendarFormatValue(all_values[vi].actual_value);
      string forecast_str = CalendarFormatValue(all_values[vi].forecast_value);
      string previous_str = CalendarFormatValue(all_values[vi].prev_value);
      string revised_str = CalendarFormatValue(all_values[vi].revision);
      
      // Check if this is a future event (actual_value is LONG_MIN = not yet published)
      bool is_future = (all_values[vi].actual_value == LONG_MIN);
      
      string evt = StringFormat(
         "{"
         "\"event_id\":\"%I64u\","
         "\"value_id\":\"%I64u\","
         "\"name\":\"%s\","
         "\"country\":\"%s\","
         "\"country_code\":\"%s\","
         "\"currency\":\"%s\","
         "\"importance\":\"%s\","
         "\"type\":\"%s\","
         "\"frequency\":\"%s\","
         "\"time\":\"%s\","
         "\"timestamp\":%I64d,"
         "\"actual\":%s,"
         "\"forecast\":%s,"
         "\"previous\":%s,"
         "\"revision\":%s,"
         "\"is_future\":%s,"
         "\"impact_type\":%d"
         "}",
         evt_def.id,
         all_values[vi].id,
         JsonEscape(evt_def.name),
         JsonEscape(evt_def.country_name),
         evt_def.country_code,
         evt_def.currency,
         CalendarImportanceToString(evt_def.importance),
         CalendarEventTypeToString(evt_def.type),
         CalendarFrequencyToString(evt_def.frequency),
         TimeToString(all_values[vi].time, TIME_DATE|TIME_SECONDS),
         (long)all_values[vi].time,
         actual_str,
         forecast_str,
         previous_str,
         revised_str,
         is_future ? "true" : "false",
         (int)all_values[vi].impact_type
      );
      
      if(events_json != "") events_json += ",";
      events_json += evt;
      event_count++;
   }
   
   datetime future = now + (hours_ahead * 3600);
   
   // Build response
   string result = StringFormat(
      "{"
      "\"events\":[%s],"
      "\"event_count\":%d,"
      "\"time_range\":{"
      "\"from\":\"%s\","
      "\"to\":\"%s\""
      "},"
      "\"source\":\"mt5_terminal_calendar\","
      "\"currency_filter\":\"%s\","
      "\"min_impact\":\"%s\","
      "\"total_events_in_cache\":%d,"
      "\"total_values_from_api\":%d,"
      "\"note\":\"Values scaled by 1,000,000 per MQL5 spec. is_future=true means actual not yet published.\""
      "}",
      events_json,
      event_count,
      TimeToString(now, TIME_DATE|TIME_SECONDS),
      TimeToString(future, TIME_DATE|TIME_SECONDS),
      currency_filter,
      min_impact,
      g_event_cache_size,
      total_values
   );
   
   Print("Calendar: Returning ", event_count, " events (from ", total_values, " values, ", g_event_cache_size, " event definitions)");
   
   return result;
}

void Complete(const string request_id, const string payload_json)
{
   // payload_json is already a JSON object string, embed it directly (not as string)
   string body = StringFormat("{\"request_id\":\"%s\",\"status\":\"ok\",\"payload\":%s}", request_id, payload_json);
   
   if(g_tcp_connected && SocketSendFrame(body))
   {
      if(EnableDebugLogs)
         Print("BridgeConnectorEA: completed request_id=", request_id, " via TCP");
      return;
   }
   
   // Fallback to HTTP
   string url = GatewayBaseURL + "/bridge/results";
   string resp;
   int code = HttpPost(url, body, resp);
   if(code != 200)
      Print("BridgeConnectorEA: result callback failed request_id=", request_id, " code=", code, " last_error=", GetLastError(), " response=", resp);
   else if(EnableDebugLogs)
      Print("BridgeConnectorEA: completed request_id=", request_id);
}

void Fail(const string request_id, const string message)
{
   string body = StringFormat("{\"request_id\":\"%s\",\"status\":\"error\",\"error\":\"%s\"}", request_id, JsonEscape(message));
   Print("BridgeConnectorEA: request failed request_id=", request_id, " error=", message);
   
   if(g_tcp_connected && SocketSendFrame(body))
      return;
   
   // Fallback to HTTP
   string url = GatewayBaseURL + "/bridge/results";
   string resp; int __code = HttpPost(url, body, resp); 
   if(__code != 200)
      Print("BridgeConnectorEA: failure callback failed request_id=", request_id, " code=", __code, " last_error=", GetLastError(), " response=", resp);
}

void ProcessCommand(const string cmd)
{
   if(cmd=="NONE" || cmd=="")
      return;
   DebugLog(StringFormat("DEBUG_PROCESSCMD: RAW_CMD=[%s]", cmd));
   string rid; if(!ParseKV(cmd, "request_id", rid)) {
      Print("BridgeConnectorEA: missing request_id in command [", cmd, "]");
      return;
   }
   string type; if(!ParseKV(cmd, "type", type)) { 
      Print("BridgeConnectorEA: missing type for request_id=", rid);
      Fail(rid, "missing_type"); 
      return; 
   }
   DebugLog(StringFormat("DEBUG_PROCESSCMD: request_id=[%s] type=[%s]", rid, type));
   if(type=="get_bars"){
      string sym; string tf; string cnts;
      if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "timeframe", tf) || !ParseKV(cmd, "count", cnts)) { Fail(rid, "bad_args"); return; }
      int cnt = (int)StringToInteger(cnts);
      string payload = JsonBars(sym, tf, cnt);
      Complete(rid, payload);
   } else if(type=="get_indicator"){
      string sym; string tf; string name; string ps;
      if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "timeframe", tf) || !ParseKV(cmd, "indicator", name)) { Fail(rid, "bad_args"); return; }
      // Try advanced first using full kv string, fallback to simple period-only
      string payload = JsonIndicatorAdvanced(sym, tf, name, cmd);
      if(StringFind(payload, "unknown_indicator")>=0 || StringFind(payload, "bad_args")>=0){
         if(!ParseKV(cmd, "period", ps)) { Fail(rid, "bad_args"); return; }
         int period = (int)StringToInteger(ps);
         payload = JsonIndicator(sym, tf, name, period);
      }
      Complete(rid, payload);
   } else if(type=="get_chart_screenshot"){
      string sym; string tf; string ws; string hs;
      if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "timeframe", tf) || !ParseKV(cmd, "width", ws) || !ParseKV(cmd, "height", hs)) { Fail(rid, "bad_args"); return; }
      int w = (int)StringToInteger(ws); int h = (int)StringToInteger(hs);
      string b64 = ScreenshotBase64(sym, tf, w, h);
      string payload = StringFormat("{\"image_base64\":\"%s\",\"content_type\":\"image/png\"}", b64);
      Complete(rid, payload);
   } else if(type=="get_ticks"){
      string sym; string cnts; if(!ParseKV(cmd, "symbol", sym)) { Fail(rid, "bad_args"); return; }
      int cnt=200; if(ParseKV(cmd, "count", cnts)) cnt=(int)StringToInteger(cnts);
      string payload = JsonTicks(sym, cnt);
      Complete(rid, payload);
   } else if(type=="get_order_book"){
      string sym; if(!ParseKV(cmd, "symbol", sym)) { Fail(rid, "bad_args"); return; }
      string payload = JsonOrderBook(sym);
      Complete(rid, payload);
   } else if(type=="get_symbol_info"){
      string sym; if(!ParseKV(cmd, "symbol", sym)) { Fail(rid, "bad_args"); return; }
      Complete(rid, JsonSymbolInfo(sym));
   } else if(type=="get_deals_history"){
      string sym; string lims; string ds;
      string symbol_filter = "";
      int limit = 100;
      int days = 30;
      if(ParseKV(cmd, "symbol", sym)) symbol_filter = sym;
      if(ParseKV(cmd, "limit", lims)) limit = (int)StringToInteger(lims);
      if(ParseKV(cmd, "days", ds)) days = (int)StringToInteger(ds);
      Complete(rid, JsonDealsHistory(symbol_filter, limit, days));
   } else if(type=="estimate_margin"){
      string sym; string side; string vols; string ps;
      if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "side", side) || !ParseKV(cmd, "volume_lots", vols)) { Fail(rid, "bad_args"); return; }
      double volume = StringToDouble(vols);
      double price_hint = 0.0;
      if(ParseKV(cmd, "price", ps)) price_hint = StringToDouble(ps);
      Complete(rid, JsonMarginEstimate(sym, side, volume, price_hint));
    } else if(type=="submit_order"){
       string sym; string side; string vols; string sls; string tps; string devs;
       if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "side", side) || !ParseKV(cmd, "volume_lots", vols)) { Fail(rid, "bad_args"); return; }
       
       // Symbol validation
       if(!EnsureSymbolInMarketWatch(sym)) { Fail(rid, "symbol_not_found"); return; }
       
       double vol = StringToDouble(vols);
       double sl = 0.0; double tp = 0.0; int dev=20;
       if(ParseKV(cmd, "sl", sls)) sl = StringToDouble(sls);
       if(ParseKV(cmd, "tp", tps)) tp = StringToDouble(tps);
       if(ParseKV(cmd, "deviation", devs)) dev = (int)StringToInteger(devs);
       
        // Ownership fields (optional, default 0 / "")
        string mns; long magic_number = 0;
        if(ParseKV(cmd, "magic_number", mns)) magic_number = (long)StringToInteger(mns);
        string order_comment = "";
        ParseKV(cmd, "comment", order_comment);

        // Trail config fields (optional, only activate when explicitly provided)
        string trail_atr_s, trail_lock_s, trail_interval_s, trail_tf_s, trail_period_s;
        bool has_trail_config = false;
        double trail_atr_multiplier = 2.0;
        double trail_lock_profit_atr = 1.0;
        int trail_check_interval = 10;
        string trail_timeframe = "H1";
        int trail_atr_period = 14;
        if(ParseKV(cmd, "trail_atr_multiplier", trail_atr_s)) { trail_atr_multiplier = StringToDouble(trail_atr_s); has_trail_config = true; }
        if(ParseKV(cmd, "trail_lock_profit_atr", trail_lock_s)) { trail_lock_profit_atr = StringToDouble(trail_lock_s); has_trail_config = true; }
        if(ParseKV(cmd, "trail_check_interval", trail_interval_s)) { trail_check_interval = (int)StringToInteger(trail_interval_s); has_trail_config = true; }
        if(ParseKV(cmd, "trail_timeframe", trail_tf_s)) { trail_timeframe = trail_tf_s; has_trail_config = true; }
         if(ParseKV(cmd, "trail_atr_period", trail_period_s)) { trail_atr_period = (int)StringToInteger(trail_period_s); has_trail_config = true; }

         // Time-based exit config fields (optional, only activate when explicitly provided)
         string max_hold_bars_s, min_profit_pts_s, hold_timeframe_s;
         bool has_time_config = false;
         int max_hold_bars = 0;
         double min_profit_points = 0.0;
         string hold_timeframe = "H1";
         if(ParseKV(cmd, "max_hold_bars", max_hold_bars_s)) { max_hold_bars = (int)StringToInteger(max_hold_bars_s); has_time_config = true; }
         if(ParseKV(cmd, "min_profit_points", min_profit_pts_s)) { min_profit_points = StringToDouble(min_profit_pts_s); has_time_config = true; }
         if(ParseKV(cmd, "hold_timeframe", hold_timeframe_s)) { hold_timeframe = hold_timeframe_s; has_time_config = true; }

        // Get current prices for SL/TP validation
       double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
       double bid = SymbolInfoDouble(sym, SYMBOL_BID);
       double point = SymbolInfoDouble(sym, SYMBOL_POINT);
       if(ask==0 || bid==0) { Fail(rid, "invalid_prices"); return; }

       // Validate SL/TP distances (minimum 10 points)
       double min_distance = 10.0 * point;
       if(sl > 0) {
          double sl_distance = MathAbs((side=="buy" ? bid - sl : sl - ask));
          if(sl_distance < min_distance) { Fail(rid, "sl_too_close"); return; }
       }
       if(tp > 0) {
          double tp_distance = MathAbs((side=="buy" ? tp - ask : bid - tp));
          if(tp_distance < min_distance) { Fail(rid, "tp_too_close"); return; }
       }

       string side_lower = side;
       StringToLower(side_lower);
       ENUM_ORDER_TYPE ot = (side_lower=="buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL);

       // Detect supported filling mode
       int filling_mask = (int)SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
       ENUM_ORDER_TYPE_FILLING filling = ORDER_FILLING_FOK;
        if((filling_mask & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK) filling = ORDER_FILLING_FOK;
        else if((filling_mask & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC) filling = ORDER_FILLING_IOC;

         // Build comment with trail and time config for recovery
         string final_comment = order_comment;
         if(has_trail_config) {
            string trail_tag = StringFormat("trail:atr=%.1f|lock=%.1f|int=%d|tf=%s|per=%d",
               trail_atr_multiplier, trail_lock_profit_atr, trail_check_interval, trail_timeframe, trail_atr_period);
            if(final_comment != "") {
               final_comment = final_comment + ";" + trail_tag;
            } else {
               final_comment = trail_tag;
            }
         }
         if(has_time_config && max_hold_bars > 0) {
            string time_tag = StringFormat("time:bars=%d|tf=%s|minprof=%.0f",
               max_hold_bars, hold_timeframe, min_profit_points);
            if(final_comment != "") {
               final_comment = final_comment + ";" + time_tag;
            } else {
               final_comment = time_tag;
            }
         }

        MqlTradeRequest req; MqlTradeResult res; ZeroMemory(req); ZeroMemory(res);
        req.action = TRADE_ACTION_DEAL;
        req.symbol = sym;
        req.volume = vol;
        req.type = ot;
        req.type_filling = filling;
        req.deviation = dev;
         req.magic = (ulong)magic_number;
        req.comment = final_comment;
        if(sl>0) req.sl = sl; if(tp>0) req.tp = tp;
        bool ok = OrderSend(req, res);
        // MANUAL VERIFICATION CHECKLIST (submit_order ownership fields):
        // 1. Verify req.magic_number matches the value sent from gateway
        // 2. Verify req.comment matches the comment string from gateway
        // 3. Confirm deal history shows correct magic_number (HistoryDealGetInteger DEAL_MAGIC)
        // 4. Confirm deal history shows correct comment (HistoryDealGetString DEAL_COMMENT)
        // 5. Test with magic_number=0 and comment="" (defaults should work)
        // 6. Test with non-zero magic_number and non-empty comment
        string payload = StringFormat("{\"retcode\":%d,\"order\":%I64d,\"deal\":%I64d,\"ask\":%G,\"bid\":%G,\"filling\":\"%s\"}", res.retcode, res.order, res.deal, ask, bid, EnumToString(filling));
       if(ok && res.retcode==10009 /*TRADE_RETCODE_DONE*/)
       {
           // Auto-start trailing if config was provided
           if(has_trail_config && res.order > 0) {
              ulong position_ticket = res.order;
              if(PositionSelectByTicket(position_ticket)) {
                 g_trailing_manager.StartTrailing(position_ticket, trail_atr_multiplier, trail_check_interval, trail_lock_profit_atr, magic_number, trail_timeframe, trail_atr_period);
              }
           }
           // Auto-register time-based exit if config was provided
           if(has_time_config && max_hold_bars > 0 && res.order > 0) {
              ulong position_ticket = res.order;
              if(PositionSelectByTicket(position_ticket)) {
                 ENUM_TIMEFRAMES ht_tf = TfFromString(hold_timeframe);
                 g_position_time_manager.RegisterPosition(position_ticket, sym, max_hold_bars, ht_tf, min_profit_points);
              }
           }
           Complete(rid, payload);
       }
       else
          Fail(rid, payload);
   } else if(type=="get_positions"){
      Complete(rid, JsonPositions());
   } else if(type=="get_orders"){
      Complete(rid, JsonOrders());
   } else if(type=="get_account"){
      Complete(rid, JsonAccount());
   } else if(type=="modify_position_sl_tp"){
      string tk; string sls; string tps;
      if(!ParseKV(cmd, "position_id", tk)) { Fail(rid, "bad_args"); return; }
      ulong ticket = (ulong)StringToInteger(tk);
      double sl=0, tp=0; if(ParseKV(cmd, "sl", sls)) sl=StringToDouble(sls); if(ParseKV(cmd, "tp", tps)) tp=StringToDouble(tps);
      bool ok = false;
      string payload = PositionModifySLTPByTicket(ticket, sl, tp, ok);
      if(ok) Complete(rid, payload); else Fail(rid, payload);
   } else if(type=="close_position"){
      string tk; string vs;
      if(!ParseKV(cmd, "position_id", tk)) { Fail(rid, "bad_args"); return; }
      double vol=0; if(ParseKV(cmd, "volume", vs)) vol=StringToDouble(vs);
      ulong ticket = (ulong)StringToInteger(tk);
      bool ok = PositionCloseByTicket(ticket, vol);
      if(ok) Complete(rid, "{\"status\":\"ok\"}"); else Fail(rid, "close_failed");
    } else if(type=="submit_pending_order"){
        string sym; string side; string kind; string ps; string vols; string sls; string tps; string devs;
        if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "side", side) || !ParseKV(cmd, "kind", kind) || !ParseKV(cmd, "price", ps) || !ParseKV(cmd, "volume_lots", vols)) { Fail(rid, "bad_args"); return; }
        if(!EnsureSymbolInMarketWatch(sym)) { Fail(rid, "symbol_not_found"); return; }
        double price = StringToDouble(ps); double vol = StringToDouble(vols);
        double sl=0, tp=0; int dev=20; if(ParseKV(cmd, "sl", sls)) sl=StringToDouble(sls); if(ParseKV(cmd, "tp", tps)) tp=StringToDouble(tps); if(ParseKV(cmd, "deviation", devs)) dev=(int)StringToInteger(devs);
        int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
        price = NormalizeDouble(price, digits);
        if(sl > 0) sl = NormalizeDouble(sl, digits);
        if(tp > 0) tp = NormalizeDouble(tp, digits);
        ResetLastError();
        CTrade trade; trade.SetDeviationInPoints(dev); trade.SetTypeFillingBySymbol(sym); int setup_err = GetLastError(); bool ok=false;
        string s = side; StringToLower(s); string k = kind; StringToLower(k);
        if(s=="buy" && k=="limit") ok=trade.BuyLimit(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, "");
        else if(s=="sell" && k=="limit") ok=trade.SellLimit(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, "");
        else if(s=="buy" && k=="stop") ok=trade.BuyStop(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, "");
        else if(s=="sell" && k=="stop") ok=trade.SellStop(vol, price, sym, sl, tp, ORDER_TIME_GTC, 0, "");
        int last_err = GetLastError();
        uint retcode = trade.ResultRetcode();
        string payload = StringFormat("{\"retcode\":%u,\"retcode_description\":\"%s\",\"order\":%I64d,\"deal\":%I64d,\"price\":%G,\"volume\":%G,\"symbol\":\"%s\",\"last_error\":%d,\"setup_error\":%d}",
           retcode,
           JsonEscape(trade.ResultRetcodeDescription()),
           trade.ResultOrder(),
           trade.ResultDeal(),
           price,
           vol,
           sym,
           last_err,
           setup_err);
        if(ok && (retcode==10008 || retcode==10009)) Complete(rid, payload); else Fail(rid, payload);
   } else if(type=="cancel_order"){
      string okid; if(!ParseKV(cmd, "order_id", okid)) { Fail(rid, "bad_args"); return; }
      ulong ticket = (ulong)StringToInteger(okid);
      bool ok = OrderDeleteByTicket(ticket);
      if(ok) Complete(rid, "{\"status\":\"ok\"}"); else Fail(rid, "cancel_failed");
   } else if(type=="modify_order"){
      string okid; string ps; string sls; string tps;
      if(!ParseKV(cmd, "order_id", okid)) { Fail(rid, "bad_args"); return; }
      double price=0, sl=0, tp=0; if(ParseKV(cmd, "new_price", ps)) price=StringToDouble(ps); if(ParseKV(cmd, "new_sl", sls)) sl=StringToDouble(sls); if(ParseKV(cmd, "new_tp", tps)) tp=StringToDouble(tps);
      ulong ticket = (ulong)StringToInteger(okid);
      bool ok = OrderModifyByTicket(ticket, price, sl, tp);
      if(ok) Complete(rid, "{\"status\":\"ok\"}"); else Fail(rid, "modify_order_failed");
   } else if(type=="close_all_positions"){
      string sym; string side;
      bool hasSym = ParseKV(cmd, "symbol", sym);
      bool hasSide = ParseKV(cmd, "side", side);
      int total = PositionsTotal(); int okc=0; int errc=0;
      for(int i=0;i<total;i++){
         ulong tk = PositionGetTicket(i);
         if(PositionSelectByTicket(tk)){
            string psym = PositionGetString(POSITION_SYMBOL);
            int ptype = (int)PositionGetInteger(POSITION_TYPE);
            string pside = (ptype==POSITION_TYPE_BUY?"buy":"sell");
            if(hasSym && psym!=sym) continue;
            if(hasSide && side!="both" && pside!=side) continue;
            if(PositionCloseByTicket(tk, 0)) okc++; else errc++;
         }
      }
      string resp = StringFormat("{\"closed\":%d,\"failed\":%d}", okc, errc);
      Complete(rid, resp);
    } else if(type=="cancel_all_orders"){
       string sym; string side;
       bool hasSym = ParseKV(cmd, "symbol", sym);
       bool hasSide = ParseKV(cmd, "side", side);
       int total = OrdersTotal(); int okc=0; int errc=0;
       for(int i=0;i<total;i++){
          ulong tk = OrderGetTicket(i);
          if(OrderSelect(tk)){
             string osym = OrderGetString(ORDER_SYMBOL);
             int otype = (int)OrderGetInteger(ORDER_TYPE);
             string oside = (otype==ORDER_TYPE_BUY||otype==ORDER_TYPE_BUY_LIMIT||otype==ORDER_TYPE_BUY_STOP)?"buy":"sell";
             if(hasSym && osym!=sym) continue;
             if(hasSide && side!="both" && oside!=side) continue;
             if(OrderDeleteByTicket(tk)) okc++; else errc++;
          }
       }
       string resp = StringFormat("{\"cancelled\":%d,\"failed\":%d}", okc, errc);
       Complete(rid, resp);
     } else if(type=="get_calendar"){
        string cur=""; string has="24"; string mi="MEDIUM";
        ParseKV(cmd, "currency", cur);
        ParseKV(cmd, "hours_ahead", has);
        ParseKV(cmd, "min_impact", mi);
        int ha = (int)StringToInteger(has);
        if(ha < 1) ha = 24;
        Complete(rid, JsonCalendar(cur, ha, mi));
      } else if(type=="trailing_start"){
         string tk; string amps; string cis; string lps; string mns;
         if(!ParseKV(cmd, "ticket", tk) || !ParseKV(cmd, "atr_multiplier", amps) || !ParseKV(cmd, "check_interval", cis)) { Fail(rid, "bad_args"); return; }
         ulong ticket = (ulong)StringToInteger(tk);
         double atr_mult = StringToDouble(amps);
         int check_interval = (int)StringToInteger(cis);
         double lock_in = 0.0;
         if(ParseKV(cmd, "lock_in_profit_atr", lps)) lock_in = StringToDouble(lps);
         long magic_filter = 0;
         if(ParseKV(cmd, "magic_filter", mns)) magic_filter = (long)StringToInteger(mns);
         string atr_timeframe = "H1";
         string atfs;
         if(ParseKV(cmd, "atr_timeframe", atfs)) atr_timeframe = atfs;
         int atr_period = 14;
         string aps;
         if(ParseKV(cmd, "atr_period", aps)) atr_period = (int)StringToInteger(aps);
         bool ok = g_trailing_manager.StartTrailing(ticket, atr_mult, check_interval, lock_in, magic_filter, atr_timeframe, atr_period);
         if(ok)
            Complete(rid, StringFormat("{\"status\":\"ok\",\"ticket\":\"%I64d\",\"atr_multiplier\":%G,\"atr_timeframe\":\"%s\",\"atr_period\":%d,\"lock_in_profit_atr\":%G}", ticket, atr_mult, atr_timeframe, atr_period, lock_in));
         else
            Fail(rid, StringFormat("{\"error\":\"trailing_start_failed\",\"ticket\":\"%I64d\"}", ticket));
     } else if(type=="trailing_stop"){
        string tk;
        if(!ParseKV(cmd, "ticket", tk)) { Fail(rid, "bad_args"); return; }
        ulong ticket = (ulong)StringToInteger(tk);
        bool ok = g_trailing_manager.StopTrailing(ticket);
        if(ok)
           Complete(rid, StringFormat("{\"status\":\"ok\",\"ticket\":\"%I64d\"}", ticket));
        else
           Fail(rid, StringFormat("{\"error\":\"trailing_stop_failed\",\"ticket\":\"%I64d\"}", ticket));
     } else if(type=="trailing_list"){
        Complete(rid, g_trailing_manager.GetActiveList());
      } else if(type=="trailing_tick"){
         int processed = g_trailing_manager.ProcessAll();
         Complete(rid, StringFormat("{\"processed\":%d,\"active\":%d}", processed, g_trailing_manager.GetActiveCount()));
       } else if(type=="bracket_start"){
          string buy_tk; string sell_tk; string bid;
          if(!ParseKV(cmd, "buy_order_ticket", buy_tk) || !ParseKV(cmd, "sell_order_ticket", sell_tk) || !ParseKV(cmd, "bracket_id", bid)) { Fail(rid, "bad_args"); return; }
          ulong buy_ticket = (ulong)StringToInteger(buy_tk);
          ulong sell_ticket = (ulong)StringToInteger(sell_tk);
          string bracket_id = bid;
          string comment = "";
          ParseKV(cmd, "comment", comment);
          long magic_filter = 0;
          string mns;
          if(ParseKV(cmd, "magic_filter", mns)) magic_filter = (long)StringToInteger(mns);

          // Retry loop: pending orders may not be immediately visible in OrdersTotal()
          // after placement. Retry up to 5 times with 50ms between attempts.
          bool ok = false;
          for(int retry = 0; retry < 5; retry++)
          {
             if(retry > 0)
             {
                Sleep(50);
                ::OrdersTotal(); // Force MT5 to refresh order cache
             }
             ok = g_bracket_manager.StartBracket(buy_ticket, sell_ticket, bracket_id, comment, magic_filter);
             if(ok) break;
          }
          if(ok)
             Complete(rid, StringFormat("{\"status\":\"ok\",\"bracket_id\":\"%s\",\"buy_ticket\":\"%I64d\",\"sell_ticket\":\"%I64d\"}", bracket_id, buy_ticket, sell_ticket));
          else
             Fail(rid, StringFormat("{\"error\":\"bracket_start_failed\",\"bracket_id\":\"%s\"}", bracket_id));
      } else if(type=="bracket_stop"){
         string bid;
         if(!ParseKV(cmd, "bracket_id", bid)) { Fail(rid, "bad_args"); return; }
         bool ok = g_bracket_manager.StopBracket(bid);
         if(ok)
            Complete(rid, StringFormat("{\"status\":\"ok\",\"bracket_id\":\"%s\"}", bid));
         else
            Fail(rid, StringFormat("{\"error\":\"bracket_stop_failed\",\"bracket_id\":\"%s\"}", bid));
      } else if(type=="bracket_list"){
         Complete(rid, g_bracket_manager.GetActiveBrackets());
       } else if(type=="bracket_tick"){
          string result = g_bracket_manager.ProcessAll();
          Complete(rid, result);
       } else if(type=="safe_shutdown"){
          string mode="full"; string sessId=""; string stratId="";
          ParseKV(cmd, "mode", mode);
          ParseKV(cmd, "session_id", sessId);
          ParseKV(cmd, "strategy_id", stratId);
          int posOk=0; int posFail=0; int ordOk=0; int ordFail=0;
          bool doFlatten = (mode=="flatten" || mode=="full");
          bool doFreeze = (mode=="freeze" || mode=="full");
          if(doFlatten){
             int total = PositionsTotal();
             for(int i=0;i<total;i++){
                ulong tk = PositionGetTicket(i);
                if(!PositionSelectByTicket(tk)) continue;
                string psid = PositionGetString(POSITION_COMMENT);
                if(sessId!="" && StringFind(psid, sessId)<0) continue;
                if(PositionCloseByTicket(tk, 0)) posOk++; else posFail++;
             }
          }
          if(doFreeze){
             int total = OrdersTotal();
             for(int i=0;i<total;i++){
                ulong tk = OrderGetTicket(i);
                if(!OrderSelect(tk)) continue;
                string osid = OrderGetString(ORDER_COMMENT);
                if(sessId!="" && StringFind(osid, sessId)<0) continue;
                if(OrderDeleteByTicket(tk)) ordOk++; else ordFail++;
             }
          }
          string resp = StringFormat("{\"mode\":\"%s\",\"positions_closed\":%d,\"positions_failed\":%d,\"orders_cancelled\":%d,\"orders_failed\":%d}",
             mode, posOk, posFail, ordOk, ordFail);
           Complete(rid, resp);
        } else if(type=="get_custom_indicator"){
           string sym; string tf; string iname; string prms; string bidx; string cnt;
           if(!ParseKV(cmd, "symbol", sym) || !ParseKV(cmd, "timeframe", tf) || 
              !ParseKV(cmd, "indicator_name", iname) || !ParseKV(cmd, "params", prms) ||
              !ParseKV(cmd, "buffer_index", bidx) || !ParseKV(cmd, "count", cnt)) { Fail(rid, "bad_args"); return; }
           string payload = JsonCustomIndicator(sym, tf, iname, prms, bidx, cnt);
           Complete(rid, payload);
        } else {
         Fail(rid, "unknown_command");
     }
  }
