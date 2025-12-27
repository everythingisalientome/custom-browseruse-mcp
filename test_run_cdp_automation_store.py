from cdp_client import ChromeCDP
import time
import json

cdp = ChromeCDP()

US_DOLLAR_XPATH = "/html/body/div/header/div[2]/div/div[2]/ul/li/a"
#APPAREL_XPATH = "//a[contains(text(), 'Apparel & accessories')]"
APPAREL_XPATH = "/html/body/div/div[1]/div[1]/section/nav/ul/li[2]/a"
TSHIRT_XPATH = "/html/body/div/div[1]/div[1]/section/nav/ul/li[2]/div/ul[1]/li[2]/a"
LOGIN_XPATH = "/html/body/div[1]/div/div[2]/div[1]/div/div/form/input"
HEADER_XPATH = "//*[text()='Swag Labs']"

print("Launching Chrome...")
cdp.launch()

print("Navigating to Automation test store...")
cdp.navigate("https://automationteststore.com/")

print("Waiting for page header 'Fast shipping'...")
cdp.wait_for_text('Fast shipping', timeout_ms=10000)

# Check viewport width
# 1. Send the command and get the ID
msg_id = cdp._send("Runtime.evaluate", {"expression": "window.innerWidth"})

# 2. Wait for the response using the ID
response = cdp._recv(msg_id)

# 3. Extract the value
current_width = response['result']['result']['value']
print(f"DEBUG: Viewport Width is {current_width}px")

#"//a[contains(., 'Apparel')]", 
debug_js = """
(function() {
    // Find ALL links containing 'Apparel'
    var snapshot = document.evaluate(
        "/html/body/div/div[1]/div[1]/section/nav/ul/li[2]/a", 
        document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
    );
    
    var results = [];
    for (var i = 0; i < snapshot.snapshotLength; i++) {
        var el = snapshot.snapshotItem(i);
        var rect = el.getBoundingClientRect();
        var style = window.getComputedStyle(el);
        results.push({
            index: i,
            text: el.innerText,
            visible: (style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0),
            parent: el.parentElement.tagName,
            outerHTML: el.outerHTML.substring(0, 100) + "..."
        });
    }
    return results;
})()
"""

msg_id = cdp._send("Runtime.evaluate", {"expression": debug_js, "returnByValue": True})
result = cdp._recv(msg_id)
print("DEBUG DOM SCAN:", json.dumps(result['result']['result']['value'], indent=2))

# Check if the Hamburger menu exists (indicates Mobile View)
is_mobile = cdp.element_exists("//div[@class='navbar-header']//button") 
if is_mobile:
    print("DEBUG: ALERT! Site is in Mobile Mode. Navigation is hidden.")

print("Hovering over 'US DOLLAR")
cdp.hover(US_DOLLAR_XPATH)
print("Selecting 'US DOLLAR'...")
cdp.click("/html/body/div/header/div[2]/div/div[2]/ul/li/ul/li[3]/a")
print("Hovering over 'Apparel & accessories'...")
cdp.hover(APPAREL_XPATH)


print("Click on 'T-Shirts'...")
cdp.click(TSHIRT_XPATH)