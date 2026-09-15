pipeline {
    agent any

    environment {
        IMAGE_NAME = "dexthida/homelab-portal"
        IMAGE_TAG  = "${env.BUILD_NUMBER}"
        DEPLOY_HOST = "192.168.100.60"
    }

    stages {
        stage('Build') {
            steps {
                sh "docker build -t ${IMAGE_NAME}:${IMAGE_TAG} -t ${IMAGE_NAME}:latest ."
            }
        }

        stage('Test') {
            steps {
                sh '''
                    docker network create portal-test-net || true

                    docker run -d --name test-db --network portal-test-net \
                        -e POSTGRES_USER=testuser \
                        -e POSTGRES_PASSWORD=testpass \
                        -e POSTGRES_DB=testdb \
                        postgres:16

                    sleep 4

                    docker run -d --name test-web --network portal-test-net \
                        -e DATABASE_URL=postgresql://testuser:testpass@test-db:5432/testdb \
                        -e SECRET_KEY=test-secret-key \
                        ${IMAGE_NAME}:${IMAGE_TAG}

                    STATUS="starting"
                    for i in $(seq 1 15); do
                        STATUS=$(docker inspect --format='{{.State.Health.Status}}' test-web 2>/dev/null || echo "starting")
                        if [ "$STATUS" = "healthy" ]; then
                            echo "Container reported healthy."
                            break
                        fi
                        sleep 2
                    done

                    if [ "$STATUS" != "healthy" ]; then
                        echo "Container never became healthy - dumping logs:"
                        docker logs test-web
                        exit 1
                    fi
                '''
            }
            post {
                always {
                    sh '''
                        docker rm -f test-web test-db || true
                        docker network rm portal-test-net || true
                    '''
                }
            }
        }

        stage('Push') {
            steps {
                withCredentials([usernamePassword(credentialsId: 'dockerhub-creds', usernameVariable: 'DOCKER_USER', passwordVariable: 'DOCKER_PASS')]) {
                    sh '''
                        echo "$DOCKER_PASS" | docker login -u "$DOCKER_USER" --password-stdin
                        docker push ${IMAGE_NAME}:${IMAGE_TAG}
                        docker push ${IMAGE_NAME}:latest
                        docker logout
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: 'docker01-deploy-key', keyFileVariable: 'DEPLOY_KEY', usernameVariable: 'DEPLOY_USER')]) {
                    sh '''
                        ssh -o StrictHostKeyChecking=no -i "$DEPLOY_KEY" "$DEPLOY_USER"@${DEPLOY_HOST} redeploy
                    '''
                }
            }
        }
    }
}
