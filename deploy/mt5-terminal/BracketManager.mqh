//+------------------------------------------------------------------+
//|                                                    BracketManager |
//|                    EA-native OCO bracket order manager             |
//|                    Survives MCP/gateway instability                |
//+------------------------------------------------------------------+
#property copyright "MetaTraderBridge"
#property version   "1.0"

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Bracket leg status                                                |
//+------------------------------------------------------------------+
enum ENUM_BRACKET_LEG_STATUS
  {
   BRACKET_LEG_PENDING,    // Order still pending
   BRACKET_LEG_FILLED,     // Order filled (deal executed)
   BRACKET_LEG_CANCELLED,  // Order cancelled
   BRACKET_LEG_UNKNOWN     // Status undetermined
  };

//+------------------------------------------------------------------+
//| CBracketManager — manages OCO bracket pairs                       |
//|                                                                  |
//| When one leg fills, the sibling is auto-cancelled.                |
//| State survives OnTimer() ticks but not EA restarts.               |
//| Recovery: scans order comments for bracket_id pattern on init.    |
//+------------------------------------------------------------------+
class CBracketManager
{
private:
   // Dynamic arrays for bracket state
   string            m_bracket_ids[];
   ulong             m_buy_tickets[];
   ulong             m_sell_tickets[];
   long              m_magic_numbers[];
   datetime          m_created_at[];
   int               m_count;

   // Global settings
   long              m_filter_magic_number;    // 0 = no filter, >0 = only manage matching magic

   CTrade            m_trade;

   //--- Find index of bracket_id in array, -1 if not found
   int               FindIndex(const string bracket_id) const;

   //--- Check if an order still exists in pending orders
   bool              OrderExists(const ulong ticket) const;

   //--- Check if an order has been filled (exists in history as deal)
   bool              OrderIsFilled(const ulong ticket) const;

   //--- Get order magic number
   long              GetOrderMagic(const ulong ticket) const;

   //--- Remove bracket at index (shift array)
   void              RemoveAt(const int idx);

   //--- Build JSON result for a bracket completion event
   string            BuildFillResult(const int idx, const string filled_leg, const ulong filled_ticket, const ulong cancelled_ticket, const double fill_price) const;

public:
                      CBracketManager();
                     ~CBracketManager();

   //--- Core API
   bool              StartBracket(ulong buy_order_ticket, ulong sell_order_ticket, const string bracket_id, const string comment = "", long magic_filter = 0);
   bool              StopBracket(const string bracket_id);
   string            ProcessAll();              // returns JSON array of fill events
   string            GetActiveBrackets() const; // returns JSON array of active brackets
   int               GetBracketCount() const;
   bool              IsActive(const string bracket_id) const;

   //--- Recovery: scan pending orders for bracket_id in comment
   int               RecoverFromOrders();

   //--- Set global magic number filter (0 = no filter)
   void              SetMagicFilter(const long magic) { m_filter_magic_number = magic; }
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
CBracketManager::CBracketManager()
{
   m_count = 0;
   m_filter_magic_number = 0;
   ArrayResize(m_bracket_ids, 32);
   ArrayResize(m_buy_tickets, 32);
   ArrayResize(m_sell_tickets, 32);
   ArrayResize(m_magic_numbers, 32);
   ArrayResize(m_created_at, 32);
}

//+------------------------------------------------------------------+
//| Destructor                                                        |
//+------------------------------------------------------------------+
CBracketManager::~CBracketManager()
{
   // Nothing to clean up
}

//+------------------------------------------------------------------+
//| Find bracket_id index                                             |
//+------------------------------------------------------------------+
int CBracketManager::FindIndex(const string bracket_id) const
{
   for(int i = 0; i < m_count; i++)
   {
      if(m_bracket_ids[i] == bracket_id)
         return i;
   }
   return -1;
}

//+------------------------------------------------------------------+
//| Check if order exists in pending orders                           |
//+------------------------------------------------------------------+
bool CBracketManager::OrderExists(const ulong ticket) const
{
   if(ticket == 0) return false;
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      ulong t = OrderGetTicket(i);
      if(t == ticket)
         return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Check if order has been filled (check deal history)               |
//+------------------------------------------------------------------+
bool CBracketManager::OrderIsFilled(const ulong ticket) const
{
   if(ticket == 0) return false;

   // Check current pending orders first — if still there, not filled
   if(OrderExists(ticket))
      return false;

   // Check history for a deal with this order ticket
   // We need to search deals — a filled order produces a deal
   datetime from = TimeCurrent() - 86400 * 7; // last 7 days
   datetime to = TimeCurrent();

   if(HistorySelect(from, to))
   {
      int total_deals = HistoryDealsTotal();
      for(int i = 0; i < total_deals; i++)
      {
         ulong deal_ticket = HistoryDealGetTicket(i);
         if(deal_ticket > 0)
         {
            ulong order_ticket = HistoryDealGetInteger(deal_ticket, DEAL_ORDER);
            if(order_ticket == ticket)
               return true; // Found a deal linked to this order — it was filled
         }
      }
   }

   return false;
}

//+------------------------------------------------------------------+
//| Get order magic number from pending orders                        |
//+------------------------------------------------------------------+
long CBracketManager::GetOrderMagic(const ulong ticket) const
{
   if(ticket == 0) return 0;
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      ulong t = OrderGetTicket(i);
      if(t == ticket)
      {
         if(OrderSelect(t))
            return (long)OrderGetInteger(ORDER_MAGIC);
      }
   }
   // Check history
   datetime from = TimeCurrent() - 86400 * 7;
   if(HistorySelect(from, TimeCurrent()))
   {
      int total_orders = HistoryOrdersTotal();
      for(int i = 0; i < total_orders; i++)
      {
         ulong h_order = HistoryOrderGetTicket(i);
         if(h_order == ticket)
            return (long)HistoryOrderGetInteger(h_order, ORDER_MAGIC);
      }
   }
   return 0;
}

//+------------------------------------------------------------------+
//| Remove bracket at index                                           |
//+------------------------------------------------------------------+
void CBracketManager::RemoveAt(const int idx)
{
   if(idx < 0 || idx >= m_count) return;

   for(int i = idx; i < m_count - 1; i++)
   {
      m_bracket_ids[i] = m_bracket_ids[i + 1];
      m_buy_tickets[i] = m_buy_tickets[i + 1];
      m_sell_tickets[i] = m_sell_tickets[i + 1];
      m_magic_numbers[i] = m_magic_numbers[i + 1];
      m_created_at[i] = m_created_at[i + 1];
   }
   m_count--;
}

//+------------------------------------------------------------------+
//| Build JSON result for a fill event                                |
//+------------------------------------------------------------------+
string CBracketManager::BuildFillResult(const int idx, const string filled_leg, const ulong filled_ticket, const ulong cancelled_ticket, const double fill_price) const
{
   return StringFormat(
      "{\"bracket_id\":\"%s\",\"filled_leg\":\"%s\",\"filled_ticket\":\"%I64d\",\"cancelled_ticket\":\"%I64d\",\"fill_price\":%G}",
      m_bracket_ids[idx],
      filled_leg,
      filled_ticket,
      cancelled_ticket,
      fill_price
   );
}

//+------------------------------------------------------------------+
//| Start tracking a bracket pair                                     |
//+------------------------------------------------------------------+
bool CBracketManager::StartBracket(ulong buy_order_ticket, ulong sell_order_ticket, const string bracket_id, const string comment = "", long magic_filter = 0)
{
   // Validate
   if(bracket_id == "")
   {
      Print("BracketManager: Empty bracket_id");
      return false;
   }
   if(buy_order_ticket == 0 && sell_order_ticket == 0)
   {
      Print("BracketManager: Both order tickets are zero");
      return false;
   }
   if(FindIndex(bracket_id) >= 0)
   {
      Print("BracketManager: Bracket already exists: ", bracket_id);
      return false;
   }

   // Validate orders exist if both provided
   if(buy_order_ticket > 0 && !OrderExists(buy_order_ticket))
   {
      Print("BracketManager: Buy order ", buy_order_ticket, " not found in pending orders");
      return false;
   }
   if(sell_order_ticket > 0 && !OrderExists(sell_order_ticket))
   {
      Print("BracketManager: Sell order ", sell_order_ticket, " not found in pending orders");
      return false;
   }

   // Check magic number filter
   if(m_filter_magic_number > 0)
   {
      if(buy_order_ticket > 0)
      {
         long buy_magic = GetOrderMagic(buy_order_ticket);
         if(buy_magic != m_filter_magic_number)
         {
            Print("BracketManager: Buy order magic ", buy_magic, " doesn't match filter ", m_filter_magic_number);
            return false;
         }
      }
      if(sell_order_ticket > 0)
      {
         long sell_magic = GetOrderMagic(sell_order_ticket);
         if(sell_magic != m_filter_magic_number)
         {
            Print("BracketManager: Sell order magic ", sell_magic, " doesn't match filter ", m_filter_magic_number);
            return false;
         }
      }
   }
   if(magic_filter > 0)
   {
      if(buy_order_ticket > 0 && GetOrderMagic(buy_order_ticket) != magic_filter)
         return false;
      if(sell_order_ticket > 0 && GetOrderMagic(sell_order_ticket) != magic_filter)
         return false;
   }

   // Expand arrays if needed
   if(m_count >= ArraySize(m_bracket_ids))
   {
      int new_size = ArraySize(m_bracket_ids) + 16;
      ArrayResize(m_bracket_ids, new_size);
      ArrayResize(m_buy_tickets, new_size);
      ArrayResize(m_sell_tickets, new_size);
      ArrayResize(m_magic_numbers, new_size);
      ArrayResize(m_created_at, new_size);
   }

   // Store
   int idx = m_count;
   m_bracket_ids[idx] = bracket_id;
   m_buy_tickets[idx] = buy_order_ticket;
   m_sell_tickets[idx] = sell_order_ticket;
   m_magic_numbers[idx] = magic_filter;
   m_created_at[idx] = TimeCurrent();
   m_count++;

   Print("BracketManager: Started bracket ", bracket_id,
         " buy=", buy_order_ticket,
         " sell=", sell_order_ticket,
         " magic_filter=", magic_filter);

   return true;
}

//+------------------------------------------------------------------+
//| Stop and remove a bracket (cancels both legs)                     |
//+------------------------------------------------------------------+
bool CBracketManager::StopBracket(const string bracket_id)
{
   int idx = FindIndex(bracket_id);
   if(idx < 0)
   {
      Print("BracketManager: Bracket not found: ", bracket_id);
      return false;
   }

   // Cancel pending legs
   if(m_buy_tickets[idx] > 0 && OrderExists(m_buy_tickets[idx]))
   {
      m_trade.OrderDelete(m_buy_tickets[idx]);
   }
   if(m_sell_tickets[idx] > 0 && OrderExists(m_sell_tickets[idx]))
   {
      m_trade.OrderDelete(m_sell_tickets[idx]);
   }

   RemoveAt(idx);
   Print("BracketManager: Stopped bracket ", bracket_id);
   return true;
}

//+------------------------------------------------------------------+
//| Process all active brackets — OCO lifecycle                       |
//| Returns JSON array of completion events                           |
//+------------------------------------------------------------------+
string CBracketManager::ProcessAll()
{
   string results = "[";
   int result_count = 0;
   int processed = 0;
   int errors = 0;

   for(int i = 0; i < m_count; i++)
   {
      string bracket_id = m_bracket_ids[i];
      ulong buy_ticket = m_buy_tickets[i];
      ulong sell_ticket = m_sell_tickets[i];

      bool buy_exists = (buy_ticket > 0) && OrderExists(buy_ticket);
      bool sell_exists = (sell_ticket > 0) && OrderExists(sell_ticket);
      bool buy_filled = (buy_ticket > 0) && !buy_exists && OrderIsFilled(buy_ticket);
      bool sell_filled = (sell_ticket > 0) && !sell_exists && OrderIsFilled(sell_ticket);

      processed++;

      // Case 1: Buy leg filled → cancel sell
      if(buy_filled && sell_exists)
      {
         double fill_price = 0;
         // Get fill price from deal history
         datetime from = TimeCurrent() - 86400 * 7;
         if(HistorySelect(from, TimeCurrent()))
         {
            int total_deals = HistoryDealsTotal();
            for(int d = 0; d < total_deals; d++)
            {
               ulong deal_ticket = HistoryDealGetTicket(d);
               if(deal_ticket > 0 && HistoryDealGetInteger(deal_ticket, DEAL_ORDER) == buy_ticket)
               {
                  fill_price = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
                  break;
               }
            }
         }

         if(m_trade.OrderDelete(sell_ticket))
         {
            if(result_count > 0) results += ",";
            results += BuildFillResult(i, "buy", buy_ticket, sell_ticket, fill_price);
            result_count++;
            Print("BracketManager: Bracket ", bracket_id, " — buy filled (", buy_ticket, "), sell cancelled (", sell_ticket, ")");
         }
         else
         {
            errors++;
            Print("BracketManager: Failed to cancel sell order ", sell_ticket, " for bracket ", bracket_id);
         }
         RemoveAt(i);
         i--;
         continue;
      }

      // Case 2: Sell leg filled → cancel buy
      if(sell_filled && buy_exists)
      {
         double fill_price = 0;
         datetime from = TimeCurrent() - 86400 * 7;
         if(HistorySelect(from, TimeCurrent()))
         {
            int total_deals = HistoryDealsTotal();
            for(int d = 0; d < total_deals; d++)
            {
               ulong deal_ticket = HistoryDealGetTicket(d);
               if(deal_ticket > 0 && HistoryDealGetInteger(deal_ticket, DEAL_ORDER) == sell_ticket)
               {
                  fill_price = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
                  break;
               }
            }
         }

         if(m_trade.OrderDelete(buy_ticket))
         {
            if(result_count > 0) results += ",";
            results += BuildFillResult(i, "sell", sell_ticket, buy_ticket, fill_price);
            result_count++;
            Print("BracketManager: Bracket ", bracket_id, " — sell filled (", sell_ticket, "), buy cancelled (", buy_ticket, ")");
         }
         else
         {
            errors++;
            Print("BracketManager: Failed to cancel buy order ", buy_ticket, " for bracket ", bracket_id);
         }
         RemoveAt(i);
         i--;
         continue;
      }

      // Case 3: Either order cancelled externally → cancel sibling, remove bracket
      if(!buy_exists && !buy_filled && buy_ticket > 0)
      {
         // Buy order gone but not filled → was cancelled externally
         if(sell_exists)
         {
            m_trade.OrderDelete(sell_ticket);
            Print("BracketManager: Bracket ", bracket_id, " — buy cancelled externally, sell cancelled");
         }
         RemoveAt(i);
         i--;
         continue;
      }
      if(!sell_exists && !sell_filled && sell_ticket > 0)
      {
         // Sell order gone but not filled → was cancelled externally
         if(buy_exists)
         {
            m_trade.OrderDelete(buy_ticket);
            Print("BracketManager: Bracket ", bracket_id, " — sell cancelled externally, buy cancelled");
         }
         RemoveAt(i);
         i--;
         continue;
      }

      // Case 4: Both orders gone → bracket complete (both filled or both cancelled)
      if(!buy_exists && !sell_exists && buy_ticket > 0 && sell_ticket > 0)
      {
         Print("BracketManager: Bracket ", bracket_id, " — both orders gone, removing");
         RemoveAt(i);
         i--;
         continue;
      }
   }

   results += "]";

   if(processed > 0)
      Print("BracketManager: ProcessAll() — processed=", processed, " events=", result_count, " errors=", errors);

   // Return summary
   return StringFormat("{\"processed\":%d,\"events\":%s,\"errors\":%d,\"active\":%d}",
                       processed, results, errors, m_count);
}

//+------------------------------------------------------------------+
//| Get active brackets as JSON                                       |
//+------------------------------------------------------------------+
string CBracketManager::GetActiveBrackets() const
{
   string out = "{\"brackets\":[";
   for(int i = 0; i < m_count; i++)
   {
      if(i > 0) out += ",";
      out += StringFormat(
         "{\"bracket_id\":\"%s\",\"buy_ticket\":\"%I64d\",\"sell_ticket\":\"%I64d\",\"magic_filter\":%I64d,\"created_at\":%I64d,\"buy_exists\":%s,\"sell_exists\":%s}",
         m_bracket_ids[i],
         m_buy_tickets[i],
         m_sell_tickets[i],
         m_magic_numbers[i],
         (long)m_created_at[i],
         OrderExists(m_buy_tickets[i]) ? "true" : "false",
         OrderExists(m_sell_tickets[i]) ? "true" : "false"
      );
   }
   out += "],\"count\":" + IntegerToString(m_count) + "}";
   return out;
}

//+------------------------------------------------------------------+
//| Get bracket count                                                 |
//+------------------------------------------------------------------+
int CBracketManager::GetBracketCount() const
{
   return m_count;
}

//+------------------------------------------------------------------+
//| Check if bracket is active                                        |
//+------------------------------------------------------------------+
bool CBracketManager::IsActive(const string bracket_id) const
{
   return FindIndex(bracket_id) >= 0;
}

//+------------------------------------------------------------------+
//| Recovery: scan pending orders for bracket_id in comment           |
//| Pattern: "bracket:<id>" in order comment                          |
//+------------------------------------------------------------------+
int CBracketManager::RecoverFromOrders()
{
   int recovered = 0;
   string bracket_map[];    // bracket_id found
   ulong buy_map[];         // corresponding buy ticket
   ulong sell_map[];        // corresponding sell ticket
   ArrayResize(bracket_map, 32);
   ArrayResize(buy_map, 32);
   ArrayResize(sell_map, 32);
   int map_count = 0;

   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(!OrderSelect(ticket)) continue;

      string comment = OrderGetString(ORDER_COMMENT);
      int bracket_pos = StringFind(comment, "bracket:");
      if(bracket_pos < 0) continue;

      // Extract bracket_id after "bracket:"
      string bracket_id = StringSubstr(comment, bracket_pos + 8);
      // Trim at space if there's more text
      int space_pos = StringFind(bracket_id, " ");
      if(space_pos >= 0)
         bracket_id = StringSubstr(bracket_id, 0, space_pos);

      if(bracket_id == "") continue;

      // Check magic filter
      if(m_filter_magic_number > 0)
      {
         long order_magic = (long)OrderGetInteger(ORDER_MAGIC);
         if(order_magic != m_filter_magic_number)
            continue;
      }

      int type = (int)OrderGetInteger(ORDER_TYPE);
      bool is_buy = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_BUY);

      // Find existing entry or create new
      int entry_idx = -1;
      for(int j = 0; j < map_count; j++)
      {
         if(bracket_map[j] == bracket_id)
         {
            entry_idx = j;
            break;
         }
      }

      if(entry_idx < 0)
      {
         if(map_count >= ArraySize(bracket_map))
         {
            int new_size = ArraySize(bracket_map) + 16;
            ArrayResize(bracket_map, new_size);
            ArrayResize(buy_map, new_size);
            ArrayResize(sell_map, new_size);
         }
         entry_idx = map_count;
         bracket_map[entry_idx] = bracket_id;
         buy_map[entry_idx] = 0;
         sell_map[entry_idx] = 0;
         map_count++;
      }

      if(is_buy)
         buy_map[entry_idx] = ticket;
      else
         sell_map[entry_idx] = ticket;
   }

   // Register recovered brackets
   for(int j = 0; j < map_count; j++)
   {
      if(buy_map[j] > 0 || sell_map[j] > 0)
      {
         StartBracket(buy_map[j], sell_map[j], bracket_map[j]);
         recovered++;
      }
   }

   if(recovered > 0)
      Print("BracketManager: Recovered ", recovered, " brackets from order comments");

   return recovered;
}
