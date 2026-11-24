#include <Arduino.h>

const int TRIG_PIN = 5;    // GPIO 5
const int ECHO_PIN = 18;   // GPIO 18
const int BUZZER_PIN = 19; // Sesuaikan dengan pin buzzer

void setup() {
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);

  // Bunyi bip 2x saat pertama nyala (tanda ESP32 & baterai aman)
  for (int i = 0; i < 2; i++) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(80);
    digitalWrite(BUZZER_PIN, LOW);
    delay(80);
  }
}

void loop() {
  // Trigger gelombang ultrasonik
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2); 
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  // Baca pantulan (timeout 30ms agar tidak nge-hang jika tidak ada pantulan)
  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  float distance = (duration * 0.0343) / 2.0;

  // Jika objek terdeteksi dalam jarak 2 cm sampai 100 cm (1 meter)
  if (duration > 0 && distance >= 2 && distance <= 100) {
    // Makin dekat jaraknya (2cm), jeda makin cepat (20ms). 
    // Makin jauh (100cm), jeda makin lambat (600ms).
    int jeda = map((int)distance, 2, 100, 20, 600);
    
    digitalWrite(BUZZER_PIN, HIGH);
    delay(40); // Durasi bunyi bip
    digitalWrite(BUZZER_PIN, LOW);
    delay(jeda); // Jeda antar bip
  } else {
    // Matikan buzzer jika di luar jangkauan atau sensor tidak merespons
    digitalWrite(BUZZER_PIN, LOW);
    delay(50);
  }
}