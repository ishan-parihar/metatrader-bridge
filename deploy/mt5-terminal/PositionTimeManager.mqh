//+------------------------------------------------------------------+
//|                                                PositionTimeManager.mqh |
//|                    EA-native position time-based exit manager      |
//|                    Auto-closes positions after max holding time    |
//|                    Survives MCP/gateway instability                |
//+------------------------------------------------------------------+
#property copyright "MetaTraderBridge"
#property version   "1.0"

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| CPositionTimeManager — manages time-based position exits          |
//|                                                                  |
//| Features:                                                          |
//| - Close position after max_hold_bars elapsed on specified timeframe |
//| - Close position when min_profit_points reached (optional)        |
//| - State survives OnTimer() ticks but not EA restarts             |
//| - Recovery from position comments on EA init                      |
//+------------------------------------------------------------------+
class CPositionTimeManager
{
private:
    // Dynamic arrays for state
    ulong             m_tickets[];
    string            m_symbols[];
    int               m_max_bars[];           // max hold bars before auto-close
    ENUM_TIMEFRAMES   m_timeframes[];         // timeframe for bar counting
    double            m_min_profit[];         // min profit in points to trigger exit (0 = disabled)
    double            m_entry_prices[];       // entry price at registration
    datetime          m_entry_times[];        // entry time at registration
    int               m_entry_bar_numbers[];  // bar number at entry time (for elapsed calc)
    int               m_count;

    CTrade            m_trade;

    //--- Find index of ticket in array, -1 if not found
    int               FindIndex(const ulong ticket) const;

    //--- Convert timeframe string to ENUM_TIMEFRAMES
    ENUM_TIMEFRAMES   TimeframeFromString(string tf) const;

    //--- Get current bar number for a symbol/timeframe
    int               GetCurrentBarNumber(const string symbol, ENUM_TIMEFRAMES tf) const;

    //--- Convert timeframe enum to string
    string            TimeframeToString(ENUM_TIMEFRAMES tf) const;

    //--- Close position and unregister
    bool              CloseAndUnregister(const int idx, const string reason);

public:
                      CPositionTimeManager();
                     ~CPositionTimeManager();

    //--- Core API
    bool              RegisterPosition(ulong ticket, string symbol, int max_hold_bars, ENUM_TIMEFRAMES hold_timeframe, double min_profit_points = 0.0);
    void              CheckAll();
    bool              UnregisterPosition(ulong ticket);
    bool              IsRegistered(ulong ticket) const;

    //--- Time health for JSON reporting
    string            GetTimeHealth(ulong ticket) const;  // returns JSON object string

    //--- Recovery: parse position comment for time config and re-register
    bool              RecoverFromComment(ulong ticket, string comment);

    //--- Get active count
    int               GetActiveCount() const;

    //--- Get all registered positions as JSON
    string            GetActiveList() const;
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
CPositionTimeManager::CPositionTimeManager()
{
    m_count = 0;
    ArrayResize(m_tickets, 64);
    ArrayResize(m_symbols, 64);
    ArrayResize(m_max_bars, 64);
    ArrayResize(m_timeframes, 64);
    ArrayResize(m_min_profit, 64);
    ArrayResize(m_entry_prices, 64);
    ArrayResize(m_entry_times, 64);
    ArrayResize(m_entry_bar_numbers, 64);
    ArrayFill(m_entry_bar_numbers, 0, ArraySize(m_entry_bar_numbers), -1);
}

//+------------------------------------------------------------------+
//| Destructor                                                        |
//+------------------------------------------------------------------+
CPositionTimeManager::~CPositionTimeManager()
{
    // No handles to release — pure state manager
}

//+------------------------------------------------------------------+
//| Convert timeframe string to ENUM_TIMEFRAMES                       |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES CPositionTimeManager::TimeframeFromString(string tf) const
{
    string s = tf;
    StringToLower(s);
    if(s == "m1")  return PERIOD_M1;
    if(s == "m5")  return PERIOD_M5;
    if(s == "m15") return PERIOD_M15;
    if(s == "m30") return PERIOD_M30;
    if(s == "h1")  return PERIOD_H1;
    if(s == "h4")  return PERIOD_H4;
    if(s == "d1")  return PERIOD_D1;
    if(s == "w1")  return PERIOD_W1;
    if(s == "mn")  return PERIOD_MN1;
    if(s == "mn1") return PERIOD_MN1;
    return PERIOD_H1; // default fallback
}

//+------------------------------------------------------------------+
//| Convert timeframe enum to string                                  |
//+------------------------------------------------------------------+
string CPositionTimeManager::TimeframeToString(ENUM_TIMEFRAMES tf) const
{
    switch(tf)
    {
        case PERIOD_M1:  return "M1";
        case PERIOD_M5:  return "M5";
        case PERIOD_M15: return "M15";
        case PERIOD_M30: return "M30";
        case PERIOD_H1:  return "H1";
        case PERIOD_H4:  return "H4";
        case PERIOD_D1:  return "D1";
        case PERIOD_W1:  return "W1";
        case PERIOD_MN1: return "MN1";
        default:         return "H1";
    }
}

//+------------------------------------------------------------------+
//| Get current bar number for a symbol/timeframe                     |
//+------------------------------------------------------------------+
int CPositionTimeManager::GetCurrentBarNumber(const string symbol, ENUM_TIMEFRAMES tf) const
{
    // Use SeriesInfoInteger to get the number of bars available
    // Then use iBarShift on current time to get the current bar index
    long bars_count = SeriesInfoInteger(symbol, tf, SERIES_BARS_COUNT);
    if(bars_count <= 0) return -1;

    datetime current_time = TimeCurrent();
    int bar_index = iBarShift(symbol, tf, current_time, false);
    if(bar_index < 0) return -1;

    // Bar number = total bars - bar index (so bar 0 = most recent, highest number = oldest)
    // We want a monotonically increasing number, so use bars_count - bar_index
    return (int)(bars_count - bar_index);
}

//+------------------------------------------------------------------+
//| Find ticket index                                                 |
//+------------------------------------------------------------------+
int CPositionTimeManager::FindIndex(const ulong ticket) const
{
    for(int i = 0; i < m_count; i++)
    {
        if(m_tickets[i] == ticket)
            return i;
    }
    return -1;
}

//+------------------------------------------------------------------+
//| Register a position for time-based exit monitoring                |
//+------------------------------------------------------------------+
bool CPositionTimeManager::RegisterPosition(ulong ticket, string symbol, int max_hold_bars, ENUM_TIMEFRAMES hold_timeframe, double min_profit_points = 0.0)
{
    // Validate parameters
    if(max_hold_bars <= 0 && min_profit_points <= 0)
    {
        Print("PositionTimeManager: At least one of max_hold_bars or min_profit_points must be > 0");
        return false;
    }
    if(max_hold_bars < 0)
    {
        Print("PositionTimeManager: max_hold_bars must be >= 0: ", max_hold_bars);
        return false;
    }

    // Verify position exists
    if(!PositionSelectByTicket(ticket))
    {
        Print("PositionTimeManager: Position not found: ", ticket);
        return false;
    }

    // Check if already registered
    if(FindIndex(ticket) >= 0)
    {
        Print("PositionTimeManager: Already registered ticket ", ticket);
        return false;
    }

    // Expand arrays if needed
    if(m_count >= ArraySize(m_tickets))
    {
        int new_size = ArraySize(m_tickets) + 32;
        ArrayResize(m_tickets, new_size);
        ArrayResize(m_symbols, new_size);
        ArrayResize(m_max_bars, new_size);
        ArrayResize(m_timeframes, new_size);
        ArrayResize(m_min_profit, new_size);
        ArrayResize(m_entry_prices, new_size);
        ArrayResize(m_entry_times, new_size);
        ArrayResize(m_entry_bar_numbers, new_size);
        ArrayFill(m_entry_bar_numbers, m_count, 32, -1);
    }

    // Store position state
    int idx = m_count;
    m_tickets[idx] = ticket;
    m_symbols[idx] = symbol;
    m_max_bars[idx] = max_hold_bars;
    m_timeframes[idx] = hold_timeframe;
    m_min_profit[idx] = min_profit_points;
    m_entry_prices[idx] = PositionGetDouble(POSITION_PRICE_OPEN);
    m_entry_times[idx] = (datetime)PositionGetInteger(POSITION_TIME);
    m_entry_bar_numbers[idx] = GetCurrentBarNumber(symbol, hold_timeframe);

    m_count++;

    Print("PositionTimeManager: Registered ticket ", ticket,
          " symbol=", symbol,
          " max_bars=", max_hold_bars,
          " tf=", TimeframeToString(hold_timeframe),
          " min_profit_pts=", min_profit_points,
          " entry_price=", m_entry_prices[idx],
          " entry_bar=", m_entry_bar_numbers[idx]);

    return true;
}

//+------------------------------------------------------------------+
//| Close position and remove from monitoring                         |
//+------------------------------------------------------------------+
bool CPositionTimeManager::CloseAndUnregister(const int idx, const string reason)
{
    ulong ticket = m_tickets[idx];
    string symbol = m_symbols[idx];

    // Close the position
    bool closed = m_trade.PositionClose(ticket);
    uint retcode = m_trade.ResultRetcode();

    if(closed && (retcode == 10009 || retcode == 10025))
    {
        Print("PositionTimeManager: Position #", ticket, " closed — ", reason);
    }
    else
    {
        Print("PositionTimeManager: Position #", ticket, " close FAILED — ", reason, " (retcode=", retcode, ")");
        // Still unregister even if close failed — position may have been closed externally
    }

    // Remove by shifting remaining entries
    for(int i = idx; i < m_count - 1; i++)
    {
        m_tickets[i] = m_tickets[i + 1];
        m_symbols[i] = m_symbols[i + 1];
        m_max_bars[i] = m_max_bars[i + 1];
        m_timeframes[i] = m_timeframes[i + 1];
        m_min_profit[i] = m_min_profit[i + 1];
        m_entry_prices[i] = m_entry_prices[i + 1];
        m_entry_times[i] = m_entry_times[i + 1];
        m_entry_bar_numbers[i] = m_entry_bar_numbers[i + 1];
    }
    m_count--;

    return closed;
}

//+------------------------------------------------------------------+
//| Check all registered positions for time/profit exit conditions    |
//+------------------------------------------------------------------+
void CPositionTimeManager::CheckAll()
{
    int processed = 0;
    int closed_time = 0;
    int closed_profit = 0;
    int errors = 0;

    for(int i = 0; i < m_count; i++)
    {
        ulong ticket = m_tickets[i];
        string symbol = m_symbols[i];

        // Check if position still exists
        if(!PositionSelectByTicket(ticket))
        {
            Print("PositionTimeManager: Position ", ticket, " no longer exists, removing");
            // Remove by shifting
            for(int j = i; j < m_count - 1; j++)
            {
                m_tickets[j] = m_tickets[j + 1];
                m_symbols[j] = m_symbols[j + 1];
                m_max_bars[j] = m_max_bars[j + 1];
                m_timeframes[j] = m_timeframes[j + 1];
                m_min_profit[j] = m_min_profit[j + 1];
                m_entry_prices[j] = m_entry_prices[j + 1];
                m_entry_times[j] = m_entry_times[j + 1];
                m_entry_bar_numbers[j] = m_entry_bar_numbers[j + 1];
            }
            m_count--;
            i--; // adjust index after removal
            continue;
        }

        processed++;
        ENUM_TIMEFRAMES tf = m_timeframes[i];
        int max_bars = m_max_bars[i];
        double min_prof = m_min_profit[i];

        // Check max hold time (bar-based)
        if(max_bars > 0)
        {
            int current_bar = GetCurrentBarNumber(symbol, tf);
            int entry_bar = m_entry_bar_numbers[i];

            if(current_bar >= 0 && entry_bar >= 0)
            {
                int bars_elapsed = current_bar - entry_bar;
                if(bars_elapsed < 0) bars_elapsed = 0; // handle bar reset

                if(bars_elapsed >= max_bars)
                {
                    string reason = StringFormat("max hold time reached (%d/%d bars on %s)", bars_elapsed, max_bars, TimeframeToString(tf));
                    CloseAndUnregister(i, reason);
                    closed_time++;
                    i--; // adjust index after removal
                    continue;
                }
            }
        }

        // Check min profit target (in points)
        if(min_prof > 0)
        {
            double entry_price = m_entry_prices[i];
            double current_price = 0;
            int pos_type = (int)PositionGetInteger(POSITION_TYPE);

            if(pos_type == POSITION_TYPE_BUY)
                current_price = SymbolInfoDouble(symbol, SYMBOL_BID);
            else
                current_price = SymbolInfoDouble(symbol, SYMBOL_ASK);

            if(current_price > 0 && entry_price > 0)
            {
                double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
                if(point > 0)
                {
                    double profit_points = 0;
                    if(pos_type == POSITION_TYPE_BUY)
                        profit_points = (current_price - entry_price) / point;
                    else
                        profit_points = (entry_price - current_price) / point;

                    if(profit_points >= min_prof)
                    {
                        string reason = StringFormat("min profit target reached (%.1f/%.1f points)", profit_points, min_prof);
                        CloseAndUnregister(i, reason);
                        closed_profit++;
                        i--; // adjust index after removal
                        continue;
                    }
                }
            }
        }
    }

    if(processed > 0)
        Print("PositionTimeManager: CheckAll() — processed=", processed, " closed_time=", closed_time, " closed_profit=", closed_profit, " errors=", errors);
}

//+------------------------------------------------------------------+
//| Unregister a position from monitoring                             |
//+------------------------------------------------------------------+
bool CPositionTimeManager::UnregisterPosition(ulong ticket)
{
    int idx = FindIndex(ticket);
    if(idx < 0)
    {
        Print("PositionTimeManager: Ticket ", ticket, " not found in monitoring list");
        return false;
    }

    // Remove by shifting remaining entries
    for(int i = idx; i < m_count - 1; i++)
    {
        m_tickets[i] = m_tickets[i + 1];
        m_symbols[i] = m_symbols[i + 1];
        m_max_bars[i] = m_max_bars[i + 1];
        m_timeframes[i] = m_timeframes[i + 1];
        m_min_profit[i] = m_min_profit[i + 1];
        m_entry_prices[i] = m_entry_prices[i + 1];
        m_entry_times[i] = m_entry_times[i + 1];
        m_entry_bar_numbers[i] = m_entry_bar_numbers[i + 1];
    }
    m_count--;

    Print("PositionTimeManager: Unregistered ticket ", ticket);
    return true;
}

//+------------------------------------------------------------------+
//| Check if ticket is being monitored                                |
//+------------------------------------------------------------------+
bool CPositionTimeManager::IsRegistered(ulong ticket) const
{
    return FindIndex(ticket) >= 0;
}

//+------------------------------------------------------------------+
//| Get time health as JSON string for a ticket                       |
//+------------------------------------------------------------------+
string CPositionTimeManager::GetTimeHealth(ulong ticket) const
{
    int idx = FindIndex(ticket);
    if(idx < 0)
    {
        return "{\"is_registered\":false}";
    }

    int bars_elapsed = 0;
    int bars_remaining = 0;

    if(m_max_bars[idx] > 0)
    {
        int current_bar = GetCurrentBarNumber(m_symbols[idx], m_timeframes[idx]);
        int entry_bar = m_entry_bar_numbers[idx];

        if(current_bar >= 0 && entry_bar >= 0)
        {
            bars_elapsed = current_bar - entry_bar;
            if(bars_elapsed < 0) bars_elapsed = 0;
            bars_remaining = m_max_bars[idx] - bars_elapsed;
            if(bars_remaining < 0) bars_remaining = 0;
        }
    }

    // Calculate current profit in points
    double current_profit_points = 0.0;
    if(PositionSelectByTicket(ticket))
    {
        double entry_price = m_entry_prices[idx];
        double current_price = 0;
        int pos_type = (int)PositionGetInteger(POSITION_TYPE);

        if(pos_type == POSITION_TYPE_BUY)
            current_price = SymbolInfoDouble(m_symbols[idx], SYMBOL_BID);
        else
            current_price = SymbolInfoDouble(m_symbols[idx], SYMBOL_ASK);

        if(current_price > 0 && entry_price > 0)
        {
            double point = SymbolInfoDouble(m_symbols[idx], SYMBOL_POINT);
            if(point > 0)
            {
                if(pos_type == POSITION_TYPE_BUY)
                    current_profit_points = (current_price - entry_price) / point;
                else
                    current_profit_points = (entry_price - current_price) / point;
            }
        }
    }

    return StringFormat(
        "{\"is_registered\":true,\"bars_elapsed\":%d,\"bars_remaining\":%d,\"min_profit_points\":%G,\"current_profit_points\":%G}",
        bars_elapsed, bars_remaining, m_min_profit[idx], current_profit_points
    );
}

//+------------------------------------------------------------------+
//| Recover time config from position comment                         |
//| Expected format: time:bars=24|tf=H1|minprof=50                    |
//+------------------------------------------------------------------+
bool CPositionTimeManager::RecoverFromComment(ulong ticket, string comment)
{
    // Find the time: tag in the comment
    int start = StringFind(comment, "time:");
    if(start < 0) return false;

    // Extract the time tag (until end of comment or next semicolon)
    string time_tag = StringSubstr(comment, start);
    int semicolon = StringFind(time_tag, ";", 5);
    if(semicolon >= 0)
        time_tag = StringSubstr(time_tag, 0, semicolon);

    // Parse KV pairs
    int max_bars = 0;
    string tf_str = "H1";
    double min_profit = 0.0;

    // Parse bars=
    int bars_pos = StringFind(time_tag, "bars=");
    if(bars_pos >= 0)
    {
        string bars_val = "";
        int pipe = StringFind(time_tag, "|", bars_pos + 5);
        if(pipe >= 0)
            bars_val = StringSubstr(time_tag, bars_pos + 5, pipe - bars_pos - 5);
        else
            bars_val = StringSubstr(time_tag, bars_pos + 5);
        max_bars = (int)StringToInteger(bars_val);
    }

    // Parse tf=
    int tf_pos = StringFind(time_tag, "tf=");
    if(tf_pos >= 0)
    {
        int pipe = StringFind(time_tag, "|", tf_pos + 3);
        if(pipe >= 0)
            tf_str = StringSubstr(time_tag, tf_pos + 3, pipe - tf_pos - 3);
        else
            tf_str = StringSubstr(time_tag, tf_pos + 3);
    }

    // Parse minprof=
    int prof_pos = StringFind(time_tag, "minprof=");
    if(prof_pos >= 0)
    {
        string prof_val = "";
        int pipe = StringFind(time_tag, "|", prof_pos + 8);
        if(pipe >= 0)
            prof_val = StringSubstr(time_tag, prof_pos + 8, pipe - prof_pos - 8);
        else
            prof_val = StringSubstr(time_tag, prof_pos + 8);
        min_profit = StringToDouble(prof_val);
    }

    if(max_bars <= 0 && min_profit <= 0)
        return false;

    // Verify position exists and get symbol
    if(!PositionSelectByTicket(ticket))
        return false;

    string symbol = PositionGetString(POSITION_SYMBOL);
    ENUM_TIMEFRAMES tf = TimeframeFromString(tf_str);

    return RegisterPosition(ticket, symbol, max_bars, tf, min_profit);
}

//+------------------------------------------------------------------+
//| Get active count                                                  |
//+------------------------------------------------------------------+
int CPositionTimeManager::GetActiveCount() const
{
    return m_count;
}

//+------------------------------------------------------------------+
//| Get all registered positions as JSON                              |
//+------------------------------------------------------------------+
string CPositionTimeManager::GetActiveList() const
{
    string out = "{\"active_time_exits\":[";
    for(int i = 0; i < m_count; i++)
    {
        if(i > 0) out += ",";
        out += StringFormat(
            "{\"ticket\":\"%I64d\",\"symbol\":\"%s\",\"max_bars\":%d,\"timeframe\":\"%s\",\"min_profit_points\":%G,\"entry_price\":%G,\"entry_bar\":%d}",
            m_tickets[i],
            m_symbols[i],
            m_max_bars[i],
            TimeframeToString(m_timeframes[i]),
            m_min_profit[i],
            m_entry_prices[i],
            m_entry_bar_numbers[i]
        );
    }
    out += "],\"count\":" + IntegerToString(m_count) + "}";
    return out;
}
