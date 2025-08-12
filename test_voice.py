import pyttsx3

print("Attempting to initialize the speech engine...")

try:
    # Initialize the engine
    engine = pyttsx3.init()
    print("Engine initialized successfully.")

    # Check properties
    rate = engine.getProperty('rate')
    volume = engine.getProperty('volume')
    print(f"Current speech rate: {rate}")
    print(f"Current volume level: {volume}")

    # The text to speak
    text_to_say = "If you can hear this message, the text to speech engine is working correctly."
    print(f"Attempting to speak: '{text_to_say}'")

    # Queue the text and run the speech command
    engine.say(text_to_say)
    engine.runAndWait()

    print("Speech test finished.")

except Exception as e:
    print(f"An error occurred: {e}")
    print("\nTroubleshooting:")
    print("- Make sure you have audio drivers installed.")
    print("- On some systems, you may need to install espeak or nsss.")