pipeline {
    agent any

    environment {
        IMAGE_NAME = "dexthida/homelab-portal"
        IMAGE_TAG  = "${env.BUILD_NUMBER}"
        DEPLOY_HOST = "100.81.105.85"
        EC2_INSTANCE_ID = "i-069323efbad2c1770"
        AWS_DEFAULT_REGION = "eu-central-1"
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
                withCredentials([
                    usernamePassword(credentialsId: 'aws-ec2-starter-creds', usernameVariable: 'AWS_ACCESS_KEY_ID', passwordVariable: 'AWS_SECRET_ACCESS_KEY'),
                    sshUserPrivateKey(credentialsId: 'ec2-deploy-key', keyFileVariable: 'DEPLOY_KEY', usernameVariable: 'DEPLOY_USER')
                ]) {
                    sh '''
                        STATE=$(aws ec2 describe-instances --instance-ids ${EC2_INSTANCE_ID} --query "Reservations[0].Instances[0].State.Name" --output text)
                        echo "Current EC2 state: $STATE"

                        if [ "$STATE" != "running" ]; then
                            echo "Instance is stopped - starting it..."
                            aws ec2 start-instances --instance-ids ${EC2_INSTANCE_ID}
                            aws ec2 wait instance-running --instance-ids ${EC2_INSTANCE_ID}
                            echo "AWS reports the instance as running - now waiting for SSH to actually respond."
                        fi

                        READY=0
for i in $(seq 1 20); do
    if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$DEPLOY_KEY" "$DEPLOY_USER"@${DEPLOY_HOST} "ready"; then
        READY=1
        echo "SSH is reachable."
        break
    fi
    echo "Not ready yet, waiting..."
    sleep 5
done

if [ "$READY" != "1" ]; then
    echo "EC2 instance never became SSH-reachable within the timeout."
    exit 1
fi

                        if [ "$READY" != "1" ]; then
                            echo "EC2 instance never became SSH-reachable within the timeout."
                            exit 1
                        fi

                        ssh -o StrictHostKeyChecking=no -i "$DEPLOY_KEY" "$DEPLOY_USER"@${DEPLOY_HOST} redeploy
                    '''
                }
            }
        }
    }
}
