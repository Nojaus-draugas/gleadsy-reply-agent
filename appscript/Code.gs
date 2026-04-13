var HEADERS = ["Timestamp","Campaign","Client ID","Lead email","Company","Original message","Classification","Confidence","Generated reply","Sending account","Status"];

function doPost(e) {
  var sheet = SpreadsheetApp.openById("1EHcJ67J3mXMp0qj8eWxoEOU89tRrqn3-ItBiBBBmRlE").getActiveSheet();
  var data = JSON.parse(e.postData.contents);
  var row = HEADERS.map(function(h) { return data[h] || ""; });
  sheet.appendRow(row);
  return ContentService.createTextOutput(JSON.stringify({status: "ok"})).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  return ContentService.createTextOutput("Reply Agent Web App is running").setMimeType(ContentService.MimeType.TEXT);
}
