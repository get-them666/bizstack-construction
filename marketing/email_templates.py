"""
Free marketing email templates for BuildStack Construction LLC
"""

TEMPLATES = {
    "monthly_newsletter": {
        "subject": "What's New in Hampton Roads Construction — {month} {year}",
        "body": """Quick update from BuildStack Construction:

✅ {project_count} permits filed in {city} this month
✅ Recent project: {project_highlight}
✅ Seasonal tip: {seasonal_tip}

Thinking about a renovation? We're offering free estimates this month.

📞 (757) 846-9275
🌐 construction.bizstackperks.com

Stay building,
Shaun @ BuildStack Construction"""
    },
    
    "permit_alert": {
        "subject": "New permit filed near you — free estimate?",
        "body": """Hi {name},

We noticed a new building permit was filed at {address}.

If you're planning work in the area, we'd love to provide a free, no-obligation estimate.

📞 (757) 846-9275
🌐 construction.bizstackperks.com

— BuildStack Construction LLC"""
    },
    
    "review_request": {
        "subject": "Quick favor? 🙏",
        "body": """Hi {name},

Loved working on your {project_type}! 

Would you mind taking 30 seconds to leave a Google review? It helps other homeowners find us.

👉 {review_link}

Thanks so much,
Shaun @ BuildStack Construction"""
    },
    
    "follow_up_no_reply": {
        "subject": "Still thinking about it?",
        "body": """Hi {name},

Just checking in — saw you were looking into {project_type}.

No pressure at all, but if you have any questions I'm happy to help. We're doing free estimates this month.

📞 (757) 846-9275

— Shaun"""
    }
}
