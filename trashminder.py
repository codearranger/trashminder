"""
TrashMinder - An AppDaemon app to monitor trash bin placement using computer vision

This app:
- Captures images from camera.front_yard every hour during the monitoring window
- Uses GPT-4o to analyze if the trash bin is near the street
- Sends Pushover notifications if the trash bin is not detected
- Operates during a configurable monitoring window (default: 3pm Wednesday to 9am Thursday)
"""

import appdaemon.plugins.hass.hassapi as hass
import requests
import base64
import json
from datetime import datetime, timedelta
from openai import OpenAI


class TrashMinder(hass.Hass):
    """AppDaemon app to monitor trash bin placement"""
    
    def initialize(self):
        """Initialize the TrashMinder app"""
        
        # Get configuration parameters
        self.camera_entity = self.args.get("camera_entity", "camera.front_yard")
        self.openai_api_key = self.args.get("openai_api_key")
        self.pushover_user_key = self.args.get("pushover_user_key")
        self.pushover_api_token = self.args.get("pushover_api_token")
        self.test_mode = self.args.get("test_mode", False)
        
        # Get schedule configuration
        self.start_day = self.args.get("start_day", "wed")
        self.start_time = self.args.get("start_time", "15:00:00")
        self.end_day = self.args.get("end_day", "thu")
        self.end_time = self.args.get("end_time", "09:00:00")
        
        # Debug logging (sanitized - no secrets)
        sanitized_args = {k: "***REDACTED***" if "key" in k.lower() or "token" in k.lower() else v for k, v in self.args.items()}
        self.log(f"DEBUG: Args received (secrets redacted): {sanitized_args}")
        self.log(f"DEBUG: test_mode value: {self.test_mode}, type: {type(self.test_mode)}")
        
        # Validate required configuration
        if not self.openai_api_key:
            self.log("ERROR: openai_api_key is required", level="ERROR")
            return
        
        if not self.pushover_user_key or not self.pushover_api_token:
            self.log("ERROR: pushover_user_key and pushover_api_token are required", level="ERROR")
            return
            
        # Initialize OpenAI client
        self.openai_client = OpenAI(api_key=self.openai_api_key)
        
        # Initialize Home Assistant entity for trash bin status
        self.entity_id = "binary_sensor.trashminder_trash_bin_present"
        self.set_state(self.entity_id, state="off", attributes={
            "friendly_name": "Trash Bin at Curb",
            "device_class": "presence",
            "icon": "mdi:trash-can",
            "last_checked": None,
            "confidence": None,
            "description": "No check performed yet"
        })
        self.log(f"Created entity: {self.entity_id}")
            
        # Set up the monitoring schedule
        self.setup_monitoring_schedule()
        
        if self.test_mode:
            self.log("TrashMinder initialized in TEST MODE - will check every minute")
        else:
            self.log("TrashMinder initialized successfully - normal weekly schedule")
    
    def setup_monitoring_schedule(self):
        """Set up the monitoring schedule based on mode"""
        
        if self.test_mode:
            # In test mode, run every minute
            self.run_every(
                self.check_trash_bin_test,
                datetime.now() + timedelta(seconds=10),
                60  # Run every 60 seconds
            )
            self.log("TEST MODE: Monitoring every 60 seconds starting in 10 seconds")
        else:
            # Normal mode: Schedule to start monitoring at configured time and day
            # Using run_daily with constrain_days for the configured start day
            self.run_daily(
                self.start_monitoring,
                self.start_time,
                constrain_days=self.start_day
            )
            self.log(f"Monitoring schedule set up - will start at {self.start_time} every {self.start_day.title()}")
    
    def check_trash_bin_test(self, kwargs):
        """Test mode version that runs every minute"""
        self.log("TEST MODE: Running trash bin check")
        # Call the regular check function
        self.check_trash_bin(kwargs)
    
    def start_monitoring(self, kwargs):
        """Start the hourly monitoring cycle"""
        
        # Map day names to weekday numbers
        day_map = {
            'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3,
            'fri': 4, 'sat': 5, 'sun': 6
        }
        
        # Verify we're on the correct day (constrain_days seems unreliable)
        current_day = datetime.now().weekday()
        expected_day = day_map.get(self.start_day.lower(), 2)
        
        if current_day != expected_day:
            self.log(f"Skipping monitoring - today is {datetime.now().strftime('%A')} but configured for {self.start_day.title()}")
            return
        
        self.log("Starting trash bin monitoring cycle")
        
        # Reset the entity state at the start of monitoring
        self.set_state(self.entity_id, state="off", attributes={
            "friendly_name": "Trash Bin at Curb",
            "device_class": "presence",
            "icon": "mdi:trash-can",
            "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": None,
            "description": "Monitoring started"
        })
        
        # Get start and end day numbers (using day_map from above)
        start_day_num = day_map.get(self.start_day.lower(), 2)  # Default to Wednesday
        end_day_num = day_map.get(self.end_day.lower(), 3)  # Default to Thursday
        
        # Parse end time to get the hour
        end_hour = int(self.end_time.split(':')[0])
        
        # Calculate total hours to monitor
        # If end day is after start day in the same week
        if end_day_num > start_day_num:
            days_diff = end_day_num - start_day_num
        else:
            # Wraps around the week (e.g., Saturday to Monday)
            days_diff = (7 - start_day_num) + end_day_num
        
        # Get current hour
        start_hour = int(self.start_time.split(':')[0])
        
        # Calculate total monitoring hours
        total_hours = (days_diff * 24) - start_hour + end_hour
        
        # Schedule hourly checks
        for hour_offset in range(total_hours):
            check_time = datetime.now() + timedelta(hours=hour_offset)
            
            # Check if we've reached the end time
            if days_diff == 0:  # Same day
                if check_time.hour >= end_hour:
                    break
            elif check_time.weekday() == end_day_num and check_time.hour >= end_hour:
                break
                
            # Use run_in with proper random parameter (single random value in seconds)
            self.run_in(
                self.check_trash_bin,
                hour_offset * 3600,  # Convert hours to seconds
                random_start=-300,  # Random offset up to 5 minutes before/after
                random_end=300
            )
        
        self.log(f"Scheduled {min(hour_offset + 1, total_hours)} hourly trash bin checks starting now")
        
        # Schedule a callback to reset the entity when monitoring ends
        end_offset_seconds = total_hours * 3600
        self.run_in(self.end_monitoring, end_offset_seconds)
        self.log(f"Scheduled monitoring end in {total_hours} hours")
    
    def check_trash_bin(self, kwargs):
        """Check if trash bin is near the street"""
        
        self.log("Starting trash bin check...")
        
        try:
            # Capture image from camera
            self.log(f"Capturing image from camera: {self.camera_entity}")
            image_data = self.capture_camera_image()
            if not image_data:
                self.log("Failed to capture camera image", level="WARNING")
                return
            
            self.log("Image captured successfully, analyzing with GPT-4o...")
            
            # Analyze image with GPT-4o
            analysis_result = self.analyze_image_with_gpt(image_data)
            trash_bin_detected = analysis_result["detected"]
            confidence = analysis_result["confidence"]
            description = analysis_result["description"]
            
            # Update Home Assistant entity with the detection status
            entity_state = "on" if trash_bin_detected else "off"
            self.set_state(self.entity_id, state=entity_state, attributes={
                "friendly_name": "Trash Bin at Curb",
                "device_class": "presence",
                "icon": "mdi:trash-can" if trash_bin_detected else "mdi:trash-can-outline",
                "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": confidence,
                "description": description,
                "detected": trash_bin_detected
            })
            self.log(f"Updated entity {self.entity_id}: state={entity_state}, confidence={confidence}")
            
            # Log the results
            if self.test_mode:
                self.log(f"TEST MODE RESULT: Detected={trash_bin_detected}, Confidence={confidence}, Description={description}")
            
            # Send notification if trash bin not detected
            if not trash_bin_detected:
                self.send_pushover_notification(confidence, description, image_data)
                self.log(f"Trash bin not detected (confidence: {confidence}) - notification sent: {description}")
            else:
                self.log(f"Trash bin detected near street (confidence: {confidence}) - all good! {description}")
                # In test mode, send notification even when detected (to test image attachment)
                if self.test_mode:
                    self.send_test_notification(confidence, description, image_data)
                
        except Exception as e:
            self.log(f"Error during trash bin check: {str(e)}", level="ERROR")
            if self.test_mode:
                import traceback
                self.log(f"TEST MODE - Full error trace: {traceback.format_exc()}", level="ERROR")
    
    def capture_camera_image(self):
        """Capture image from the specified camera entity"""
        
        try:
            import requests
            import os
            
            # Get the Home Assistant token
            ha_token = os.environ.get('SUPERVISOR_TOKEN')
            if not ha_token:
                self.log("No SUPERVISOR_TOKEN found", level="ERROR")
                return None
            
            # Make direct API call to get camera image
            url = f"http://supervisor/core/api/camera_proxy/{self.camera_entity}"
            headers = {
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json"
            }
            
            self.log(f"Fetching image from camera API: {url}")
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                image_data = response.content
                self.log(f"Successfully captured image ({len(image_data)} bytes)")
                return image_data
            else:
                self.log(f"Camera API returned status {response.status_code}: {response.text}", level="ERROR")
                return None
            
        except Exception as e:
            self.log(f"Error capturing camera image: {str(e)}", level="ERROR")
            return None
    
    def analyze_image_with_gpt(self, image_data):
        """Analyze the image using GPT-4o to detect trash bin placement"""
        
        try:
            # Encode image to base64
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            # Use OpenAI client to analyze the image with structured JSON output
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """Analyze this image and determine if there is a trash bin/garbage can positioned for pickup on THIS property (the side of the street where the camera is located).

IMPORTANT: Only detect bins that are:
- On the SAME side of the street as the camera (not across the street)
- In the foreground/near side of the image (not on neighboring properties across the street)
- Positioned at or near the curb/street edge for collection
- Clearly intended for pickup from THIS property

Look for:
- Wheeled garbage bins or trash cans on our property's curb
- Bins positioned at the street edge in front of this house
- Typical residential waste containers ready for collection

Do NOT count bins that are:
- Across the street at other properties
- On neighboring properties
- In the background on the opposite side of the street

Return a JSON response indicating whether a trash bin from THIS property is positioned for pickup."""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "trash_bin_detection",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "trash_bin_present": {
                                    "type": "boolean",
                                    "description": "True if a trash bin from THIS property (not across the street) is positioned at the curb for pickup, False otherwise"
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "Confidence level of the detection"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Brief description of what was observed"
                                }
                            },
                            "required": ["trash_bin_present", "confidence", "description"],
                            "additionalProperties": False
                        },
                        "strict": True
                    }
                },
                max_tokens=100
            )
            
            # Parse the JSON response
            result = json.loads(response.choices[0].message.content)
            
            trash_bin_detected = result["trash_bin_present"]
            confidence = result["confidence"]
            description = result["description"]
            
            self.log(f"GPT-4o analysis result: trash_bin_present={trash_bin_detected}, confidence={confidence}, description='{description}'")
            
            # Return structured result
            return {
                "detected": trash_bin_detected,
                "confidence": confidence,
                "description": description
            }
                
        except Exception as e:
            self.log(f"Error analyzing image with GPT: {str(e)}", level="ERROR")
            # Return safe default on error to avoid false alarms
            return {
                "detected": True,
                "confidence": "low", 
                "description": f"Analysis failed: {str(e)}"
            }
    
    def send_pushover_notification(self, confidence="unknown", description="", image_data=None):
        """Send notification via Pushover that trash bin is not detected"""
        
        try:
            current_time = datetime.now().strftime("%I:%M %p")
            
            # Create detailed message with AI analysis info
            message = f"Trash bin not detected near the street as of {current_time}.\n\n"
            message += f"AI Analysis: {description}\n"
            message += f"Confidence: {confidence.title()}\n\n"
            message += "Don't forget to put it out for pickup!"
            
            payload = {
                "token": self.pushover_api_token,
                "user": self.pushover_user_key,
                "title": "🗑️ Trash Bin Reminder",
                "message": message,
                "priority": 1,  # High priority
                "sound": "pushover"
            }
            
            # Prepare files for attachment if image data is provided
            files = None
            if image_data:
                files = {
                    "attachment": ("camera_snapshot.jpg", image_data, "image/jpeg")
                }
                self.log("Including camera image with Pushover notification")
            
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data=payload,
                files=files,
                timeout=10
            )
            
            if response.status_code == 200:
                self.log("Pushover notification sent successfully")
            else:
                self.log(f"Pushover notification failed: {response.status_code} - {response.text}", level="ERROR")
                
        except Exception as e:
            self.log(f"Error sending Pushover notification: {str(e)}", level="ERROR")
    
    def send_test_notification(self, confidence="unknown", description="", image_data=None):
        """Send a test notification in test mode to verify image attachment works"""
        
        try:
            current_time = datetime.now().strftime("%I:%M %p")
            
            # Create test message
            message = f"🧪 TEST MODE: Trash bin detected at {current_time}.\n\n"
            message += f"AI Analysis: {description}\n"
            message += f"Confidence: {confidence.title()}\n\n"
            message += "This is a test notification with camera image attached."
            
            payload = {
                "token": self.pushover_api_token,
                "user": self.pushover_user_key,
                "title": "🧪 TrashMinder Test",
                "message": message,
                "priority": 0,  # Normal priority for tests
                "sound": "pushover"
            }
            
            # Prepare files for attachment if image data is provided
            files = None
            if image_data:
                files = {
                    "attachment": ("test_camera_snapshot.jpg", image_data, "image/jpeg")
                }
                self.log("Including camera image with test Pushover notification")
            
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data=payload,
                files=files,
                timeout=10
            )
            
            if response.status_code == 200:
                self.log("Test Pushover notification sent successfully")
            else:
                self.log(f"Test Pushover notification failed: {response.status_code} - {response.text}", level="ERROR")
            
        except Exception as e:
            self.log(f"Error sending test Pushover notification: {str(e)}", level="ERROR")
    
    def end_monitoring(self, kwargs):
        """Reset the entity state when monitoring window ends"""
        self.log("Monitoring window ended, resetting trash bin status")
        self.set_state(self.entity_id, state="off", attributes={
            "friendly_name": "Trash Bin at Curb",
            "device_class": "presence",
            "icon": "mdi:trash-can-outline",
            "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": None,
            "description": "Monitoring window ended"
        })
        self.log(f"Reset entity {self.entity_id} to off state")
    
    def terminate(self):
        """Clean up when app is terminated"""
        self.log("TrashMinder app terminated")
