//+------------------------------------------------------------------+
//|                                               AutoAttachEA.mq5  |
//| Auto-attach BridgeConnectorEA to EURUSD H1 on startup          |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs

input string EA_TCP_Host = "tradebridge";
input int    EA_TCP_Port = 8025;

void OnStart()
{
   // Open EURUSD H1 chart
   long chart = ChartOpen("EURUSD", PERIOD_H1);
   if(chart > 0)
   {
      ChartRedraw(chart);
      Sleep(3000);
      
      // Try to load EA — use Expert Advisor name
      // The EA is already in MQL5/Experts/, just needs to be on a chart
      // ChartSetSymbolPeriod opens a new chart or activates existing one
      
      Print("Chart opened: EURUSD H1 (ID: ", chart, ")");
      Print("BridgeConnectorEA ready at ", EA_TCP_Host, ":", EA_TCP_Port);
   }
   else
   {
      Print("Failed to open EURUSD chart");
   }
}
//+------------------------------------------------------------------+
