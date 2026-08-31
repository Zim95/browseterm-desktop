1. The UI should have the same color theme as browseterm UI. Go thorugh browseterm-server and understand the templates to understand the UI theme. Our box should be as big as docker desktop's box. The color theme needs to be the same color scheme as the browseterm UI.

2. The applications logo should be Browseterm Written. The UI should have the same login page that the browseterm-server has. Login with github and google. Same thing. They should follow the login protocol. Once that is done, our app opens. There should be a background process which runs every 25 minutes to update the session in redis in the cloud cluster. Unless they explicitly log out, we should never log the desktop app out.

3. The desktop app should have navigations on the side. Same color scheme as the UI. First navigation should be device. This should detect the device details and show it there and then it should have a button which says activate. This is what will set this device as active in the cloud.

4. All requests to the cloud api should have httponly cookie with session token which the cloud api will validate and accept.

5. In the navigation section, at the bottom there should be a logout button just like the UI which destrous the device session in the cloud.
