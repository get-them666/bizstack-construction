"""
Review request automation for BuildStack Construction LLC
Add this to your app to auto-request reviews after job completion
"""

REVIEW_REQUEST_TEXT = """Hi {customer_name}! 

Loved working on your {project_type}. Would you mind leaving a quick Google review? Takes 30 seconds and really helps us out!

{review_link}

Thanks!
— Shaun @ BuildStack Construction"""

REVIEW_REQUEST_EMAIL = """Subject: Quick favor? 🙏

Hi {customer_name},

Hope you're enjoying your {project_type}!

Would you mind taking 30 seconds to leave a Google review? It helps other homeowners find us.

👉 {review_link}

Thanks so much,
Shaun @ BuildStack Construction LLC
(757) 846-9275
"""

def get_review_link():
    """
    Get your Google review link:
    1. Go to google.com/search?q=BuildStack+Construction+LLC
    2. Click "Write a review"
    3. Copy the URL
    4. Paste it here
    """
    return "https://www.google.com/search?q=BuildStack+Construction+LLC"  # Replace with actual review link

def send_review_request(customer_name, project_type, phone=None, email=None):
    """Send review request via SMS or email"""
    review_link = get_review_link()
    
    if phone:
        message = REVIEW_REQUEST_TEXT.format(
            customer_name=customer_name,
            project_type=project_type,
            review_link=review_link
        )
        # Integrate with your SMS system
        print(f"SMS to {phone}: {message}")
        return message
    
    if email:
        message = REVIEW_REQUEST_EMAIL.format(
            customer_name=customer_name,
            project_type=project_type,
            review_link=review_link
        )
        # Integrate with your email system
        print(f"Email to {email}: {message}")
        return message
    
    return None

# Example usage:
# send_review_request("John Smith", "kitchen remodel", phone="+17575551234")
# send_review_request("Jane Doe", "bathroom renovation", email="jane@example.com")
